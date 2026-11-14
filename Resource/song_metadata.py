"""Bounded MP3 extraction. Never log signed URLs or embedded private data."""
import logging
import re
import time
import uuid
from io import BytesIO
from tempfile import SpooledTemporaryFile
from threading import BoundedSemaphore, Thread

import qiniu
import requests
from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp3 import MP3
from PIL import Image

from Base.qn_manager import qn_res_manager
from Resource.models import AudioMetadata, Resource, Song, SongCover
from Resource.validators import RtypeChoice

logger = logging.getLogger(__name__)
MAX_AUDIO = 64 * 1024 * 1024
MAX_LYRICS = 100_000
slots = BoundedSemaphore(2)


def is_audio(resource):
    return resource.rtype == RtypeChoice.FILE and (
        (resource.mime or '').startswith('audio/') or resource.rname.lower().endswith('.mp3'))


def lyrics_data(text, kind='text'):
    if not isinstance(text, str) or len(text) > MAX_LYRICS:
        raise ValueError('歌词过长')
    if kind not in ('none', 'text', 'lrc'):
        raise ValueError('歌词格式无效')
    cues = []
    if kind == 'lrc':
        for line in text.splitlines():
            stamps = list(re.finditer(r'\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]', line))
            content = re.sub(r'\[[^]]*\]', '', line).strip()
            for stamp in stamps:
                minutes, seconds, fraction = stamp.groups()
                if int(seconds) >= 60:
                    raise ValueError('LRC 时间格式无效')
                cues.append({'time_ms': (int(minutes) * 60 + int(seconds)) * 1000 +
                             int((fraction or '').ljust(3, '0')), 'text': content})
        if text.strip() and not cues:
            raise ValueError('LRC 缺少时间标签')
    return {'type': kind if text.strip() else 'none', 'text': text, 'cues': sorted(cues, key=lambda cue: cue['time_ms'])}


def image_bytes(data):
    if len(data) > 8 * 1024 * 1024:
        raise ValueError('封面超过 8 MB')
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > 20_000_000:
            raise ValueError('封面尺寸过大')
        image.thumbnail((1200, 1200))
        output = BytesIO()
        image.convert('RGB').save(output, 'JPEG', quality=88)
        return output.getvalue()


def parse_mp3(stream):
    result = {'title': '', 'artists': [], 'album': '', 'duration_ms': None,
              'lyrics': lyrics_data(''), 'plain_lyrics': [], 'synchronized_lyrics': []}
    artwork = None
    frame_ms = None
    stream.seek(0)
    try:
        info = MP3(stream).info
        result['duration_ms'] = round(info.length * 1000)
        frame_ms = (1152 if info.version == 1 else 576) * 1000 / info.sample_rate
    except Exception:
        pass
    stream.seek(0)
    try:
        tags = ID3(stream)  # Converts v2.2/v2.3 to v2.4 and supports v1 fallback.
    except ID3NoHeaderError:
        return result, None
    def values(key):
        return [str(value)[:256] for frame in tags.getall(key) for value in frame.text][:32]
    result['title'] = next(iter(values('TIT2')), '')
    result['artists'] = values('TPE1')
    result['album'] = next(iter(values('TALB')), '')
    for frame in tags.getall('USLT')[:8]:
        if len(frame.text) <= MAX_LYRICS:
            try:
                parsed = lyrics_data(frame.text, 'lrc' if re.search(r'\[\d+:\d+', frame.text) else 'text')
            except ValueError:
                parsed = lyrics_data(frame.text)
            result['plain_lyrics'].append({'language': frame.lang, **parsed})
    for frame in tags.getall('SYLT')[:8]:
        if (frame.format == 2 or (frame.format == 1 and frame_ms)) and len(frame.text) <= 5000:
            cues = [{'time_ms': int(stamp if frame.format == 2 else stamp * frame_ms), 'text': text} for text, stamp in frame.text
                    if stamp >= 0]
            if sum(len(cue['text']) for cue in cues) <= MAX_LYRICS:
                result['synchronized_lyrics'].append({'type': 'synchronized', 'language': frame.lang,
                    'text': '', 'cues': sorted(cues, key=lambda cue: cue['time_ms'])})
    if result['synchronized_lyrics']:
        result['lyrics'] = result['synchronized_lyrics'][0]
    elif result['plain_lyrics']:
        result['lyrics'] = result['plain_lyrics'][0]
    pictures = sorted(tags.getall('APIC'), key=lambda frame: frame.type != 3)
    for picture in pictures:
        try:
            artwork = image_bytes(picture.data)
            break
        except Exception:
            continue
    return result, artwork


def extract(metadata_id):
    """Claim a pending job, with stale claims recoverable after process restart."""
    with transaction.atomic():
        metadata = AudioMetadata.objects.select_for_update().get(pk=metadata_id)
        if metadata.status in ('ready', 'failed'):
            return
        if metadata.status == 'parsing' and (timezone.now() - metadata.updated_at).total_seconds() < 120:
            return
        metadata.status = 'parsing'
        metadata.save()
        resource = metadata.audio
    try:
        if resource.rsize > MAX_AUDIO:
            raise ValueError('size_limit')
        start = time.monotonic()
        url = qn_res_manager.get_resource_url(resource.dlpath, expires=300)
        with SpooledTemporaryFile(max_size=2 * 1024 * 1024) as stream:
            with requests.get(url, stream=True, timeout=(3, 8), allow_redirects=False) as response:
                response.raise_for_status()
                size = 0
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > MAX_AUDIO or time.monotonic() - start > 45:
                        raise ValueError('extraction_limit')
                    stream.write(chunk)
            result, artwork = parse_mp3(stream)
        # Artwork failures must not discard usable text metadata.
        if artwork:
            try:
                key = 'disk/song-covers/' + str(uuid.uuid4()) + '.jpg'
                cover, _ = SongCover.objects.get_or_create(metadata_id=metadata_id,
                    defaults={'storage_key': key, 'mime': 'image/jpeg'})
                token = qn_res_manager.auth.upload_token(qn_res_manager.bucket, cover.storage_key, expires=300)
                _, info = qiniu.put_data(token, cover.storage_key, artwork, mime_type='image/jpeg')
                if info.status_code == 200:
                    updated = SongCover.objects.filter(pk=cover.pk, metadata_id=metadata_id).update(ready=True)
                    if not updated:
                        qn_res_manager.delete_res(cover.storage_key, missing_ok=True)
            except Exception:
                logger.warning('Song artwork extraction failed for metadata %s', metadata_id)
        AudioMetadata.objects.filter(pk=metadata_id).update(status='ready', data=result, updated_at=timezone.now())
        Song.objects.filter(audio_id=resource.pk, duration_ms__isnull=True).update(duration_ms=result['duration_ms'])
        Song.objects.filter(audio_id=resource.pk).update(revision=F('revision') + 1, updated_at=timezone.now())
    except Exception:
        AudioMetadata.objects.filter(pk=metadata_id).update(status='failed', updated_at=timezone.now())
        logger.warning('Audio extraction failed for metadata %s', metadata_id)


def schedule(resource):
    if not is_audio(resource):
        return
    metadata, _ = AudioMetadata.objects.get_or_create(audio=resource)
    def start():
        if not slots.acquire(blocking=False):
            return  # Durable pending job is picked up by process_audio_metadata.
        def run():
            close_old_connections()
            try:
                extract(metadata.pk)
            finally:
                close_old_connections()
                slots.release()
        try:
            Thread(target=run, daemon=True, name='pocklo-audio-metadata').start()
        except Exception:
            slots.release()
            logger.warning('Audio metadata worker could not start for metadata %s', metadata.pk)
    transaction.on_commit(start)
