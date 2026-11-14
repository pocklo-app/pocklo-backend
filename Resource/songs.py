"""Versioned song API. Playback is always a short-lived private CDN URL."""
import json
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.views import View

from Base.auth import Auth
from Base.qn_manager import qn_res_manager
from Resource.models import AudioMetadata, Resource, Share, Song, SongCover
from Resource.song_metadata import is_audio, lyrics_data, schedule
from Resource.validators import RtypeChoice
from Resource.workspace import reply

CONTRACT = 'pocklo.song.v1'
TTL = 300


class SongError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def fail(code, status=400):
    raise SongError(code, status)


class SongView(View):
    def dispatch(self, request, *args, **kwargs):
        try:
            return super().dispatch(request, *args, **kwargs)
        except SongError as error:
            response = reply({'error': error.code})
            response.status_code = error.status
            return response


def owner(request):
    try:
        Auth._extract_user(request)
    except Exception:
        fail('login_required', 401)
    return request.user


def resource_for(user, value, kind):
    if not isinstance(value, str) or len(value) > 64:
        fail('invalid_resource_id')
    resource = Resource.objects.filter(res_str_id=value, owner=user, rtype=RtypeChoice.FILE).first()
    if not resource:
        fail('resource_not_found', 404)
    if kind == 'audio' and not is_audio(resource):
        fail('audio_required')
    if kind == 'cover' and resource.mime not in ('image/png', 'image/jpeg', 'image/webp'):
        fail('image_required')
    return resource


def body(request):
    if len(request.body) > 256_000:
        fail('metadata_too_large', 413)
    try:
        value = json.loads(request.body)
    except (ValueError, UnicodeError):
        fail('invalid_json')
    if not isinstance(value, dict):
        fail('invalid_json')
    return value


def owned_song(request, uid):
    user = owner(request)
    type(user).objects.select_for_update().get(pk=user.pk)
    song = Song.objects.select_for_update().filter(uid=uid, owner=user).first()
    if not song:
        fail('song_not_found', 404)
    return song


def song_for_share(token):
    share = Share.objects.select_related('song__audio', 'song__cover').filter(token=token, song__isnull=False).first()
    if not share:
        fail('share_not_found', 404)
    return share


def cover_info(song):
    if song.cover_id:
        return {'id': song.cover.res_str_id, 'mime': song.cover.mime}
    if song.use_embedded_cover and song.audio_id:
        asset = SongCover.objects.filter(metadata__audio_id=song.audio_id, ready=True).first()
        if asset:
            return {'id': str(asset.uid), 'mime': asset.mime}
    return None


def contract(song, share=None):
    return {
        'schema': CONTRACT, 'type': 'song', 'song_id': str(song.uid),
        'version': song.revision, 'available': bool(song.audio_id and song.audio.dlpath),
        'audio_resource_id': song.audio.res_str_id if song.audio_id else None,
        'title': song.title, 'artists': song.artists, 'album': song.album,
        'duration_ms': song.duration_ms, 'cover': cover_info(song),
        'lyrics': {'id': str(song.lyrics_id), **(song.lyrics or lyrics_data(''))},
        'cover_source': 'resource' if song.cover_id else 'embedded' if song.use_embedded_cover else None,
        'share_url': f'{settings.POCKLO_OAUTH_WEB_ORIGIN}/share/{share.token}' if share else None,
    }


def apply(song, data):
    allowed = {'title', 'artists', 'album', 'duration_ms', 'audio_resource_id', 'cover_resource_id',
               'use_embedded_cover', 'lyrics', 'version'}
    if set(data) - allowed:
        fail('unknown_field')
    for field in ('title', 'album'):
        if field in data:
            value = data[field]
            if not isinstance(value, str) or len(value) > 256 or (field == 'title' and not value.strip()):
                fail('invalid_' + field)
            setattr(song, field, value.strip())
    if 'artists' in data:
        values = data['artists']
        if not isinstance(values, list) or len(values) > 32 or any(
                not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
            fail('invalid_artists')
        song.artists = [v.strip() for v in values]
    if 'duration_ms' in data:
        value = data['duration_ms']
        if value is not None and (type(value) is not int or value < 0 or value > 86400000):
            fail('invalid_duration')
        song.duration_ms = value
    if 'audio_resource_id' in data:
        previous = song.audio_id
        song.audio = resource_for(song.owner, data['audio_resource_id'], 'audio')
        if song.pk and previous != song.audio_id and 'duration_ms' not in data:
            metadata = AudioMetadata.objects.filter(audio=song.audio, status='ready').first()
            song.duration_ms = metadata.data.get('duration_ms') if metadata else None
        if Song.objects.filter(audio=song.audio).exclude(pk=song.pk).exists():
            fail('audio_already_has_song', 409)
    if 'cover_resource_id' in data:
        song.cover = resource_for(song.owner, data['cover_resource_id'], 'cover') if data['cover_resource_id'] else None
    if 'use_embedded_cover' in data:
        if type(data['use_embedded_cover']) is not bool:
            fail('invalid_cover')
        song.use_embedded_cover = data['use_embedded_cover']
    if 'lyrics' in data:
        value = data['lyrics']
        if not isinstance(value, dict):
            fail('invalid_lyrics')
        try:
            song.lyrics = lyrics_data(value.get('text', ''), value.get('type', 'none'))
        except (ValueError, TypeError):
            fail('invalid_lyrics')


class AudioSuggestions(SongView):
    def get(self, request, resource_id):
        audio = resource_for(owner(request), resource_id, 'audio')
        metadata = AudioMetadata.objects.filter(audio=audio).first()
        if not metadata:
            schedule(audio)
            metadata = AudioMetadata.objects.get(audio=audio)
        song = Song.objects.filter(audio=audio).first()
        artwork = SongCover.objects.filter(metadata=metadata, ready=True).first()
        cover_key = song.cover.dlpath if song and song.cover_id else (
            artwork.storage_key if artwork and (not song or song.use_embedded_cover) else None)
        return reply({'status': metadata.status, 'extracted': metadata.data,
                      'has_embedded_cover': bool(artwork),
                      'cover_preview_url': qn_res_manager.get_resource_url(cover_key, expires=TTL) if cover_key else None,
                      'song': contract(song) if song else None})

    def post(self, request, resource_id):
        audio = resource_for(owner(request), resource_id, 'audio')
        AudioMetadata.objects.filter(audio=audio, status='failed').update(status='pending')
        schedule(audio)
        return reply({'status': 'pending'})


class Songs(SongView):
    @transaction.atomic
    def post(self, request):
        user = owner(request)
        # Serialize creation with the same owner lock used for structure edits.
        type(user).objects.select_for_update().get(pk=user.pk)
        data = body(request)
        audio = resource_for(user, data.get('audio_resource_id'), 'audio')
        if Song.objects.filter(audio=audio).exists():
            fail('audio_already_has_song', 409)
        metadata = AudioMetadata.objects.filter(audio=audio, status='ready').first()
        extracted = metadata.data if metadata else {}
        song = Song(owner=user, audio=audio, title=extracted.get('title') or audio.rname.rsplit('.', 1)[0],
                    artists=extracted.get('artists', []), album=extracted.get('album', ''),
                    duration_ms=extracted.get('duration_ms'), lyrics=extracted.get('lyrics', lyrics_data('')))
        apply(song, data)
        song.save()
        return reply(contract(song))


class SongDetail(SongView):
    @transaction.atomic
    def get(self, request, uid):
        return reply(contract(owned_song(request, uid)))

    @transaction.atomic
    def put(self, request, uid):
        song = owned_song(request, uid)
        data = body(request)
        if type(data.get('version')) is not int or data['version'] != song.revision:
            fail('version_conflict', 409)
        apply(song, data)
        song.revision += 1
        song.save()
        return reply(contract(song))

    @transaction.atomic
    def delete(self, request, uid):
        owned_song(request, uid).delete()
        return reply(None)


@transaction.atomic
def create_share(resource):
    type(resource.owner).objects.select_for_update().get(pk=resource.owner_id)
    song = Song.objects.filter(audio=resource).first()
    return Share.objects.create(song=song) if song else Share.objects.create(resource=resource)


class SongSharing(SongView):
    @transaction.atomic
    def post(self, request, uid):
        song = owned_song(request, uid)
        if not song.audio_id:
            fail('audio_unavailable', 410)
        share = Share.objects.create(song=song)
        return reply({'token': str(share.token), **contract(song, share)})


class PublicSong(SongView):
    def get(self, request, token):
        share = song_for_share(token)
        return reply(contract(share.song, share))


class SongPlayback(SongView):
    def get(self, request, token):
        share = song_for_share(token)
        song = share.song
        if not song.audio_id or not song.audio.dlpath:
            fail('audio_unavailable', 410)
        result = {'schema': CONTRACT, 'song_id': str(song.uid), 'version': song.revision,
                  'expires_at': (timezone.now() + timedelta(seconds=TTL)).isoformat(),
                  'expires_in': TTL,
                  'audio': {'resource_id': song.audio.res_str_id, 'mime': song.audio.mime or 'audio/mpeg',
                            'url': qn_res_manager.get_resource_url(song.audio.dlpath, expires=TTL)},
                  'cover': None}
        if song.cover_id:
            key = song.cover.dlpath
        else:
            asset = SongCover.objects.filter(metadata__audio_id=song.audio_id, ready=True).first() if song.use_embedded_cover else None
            key = asset.storage_key if asset else None
        if key:
            result['cover'] = {**cover_info(song), 'url': qn_res_manager.get_resource_url(key, expires=TTL)}
        return reply(result)
