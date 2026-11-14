from io import BytesIO
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone
from mutagen.id3 import ID3, TIT2, TPE1, TALB, USLT, SYLT, APIC
from PIL import Image

from Base.auth import Auth
from Resource.models import Resource, Song, Share, AudioMetadata, SongCover
from Resource.song_metadata import parse_mp3, lyrics_data, extract
from Resource.validators import RtypeChoice, StypeChoice
from Resource.workspace import entry
from User.models import User


@patch('Base.jtoken.SECRET_KEY', 'test-only-song-key-at-least-32-characters')
@patch('Base.jtoken.JWT_ENCODE_ALGO', 'HS256')
class SongTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(qt_user_app_id='song-owner', qtb_token='')
        self.other = User.objects.create(qt_user_app_id='song-other', qtb_token='')
        self.system = self.resource('system', folder=True)
        self.root = self.resource('root', folder=True, parent=self.system)
        self.audio = self.resource('music', parent=self.root, mime='audio/mpeg')
        self.private = self.resource('private', parent=self.root)
        self.cover = self.resource('cover', parent=self.root, mime='image/jpeg')
        self.song = Song.objects.create(owner=self.owner, audio=self.audio, title='Test Song', artists=['Artist'],
                                      lyrics=lyrics_data('[00:01.20]Hi', 'lrc'))
        self.share = Share.objects.create(song=self.song)

    def resource(self, slug, parent=None, folder=False, mime='text/plain'):
        return Resource.objects.create(owner=self.owner, parent=parent, res_str_id=slug,
            rname=slug + ('.mp3' if mime == 'audio/mpeg' else ''), mime=mime,
            rtype=RtypeChoice.FOLDER if folder else RtypeChoice.FILE,
            sub_type=StypeChoice.MUSIC if mime == 'audio/mpeg' else StypeChoice.FILE,
            create_time=timezone.now(), dlpath=None if folder else 'disk/res/' + slug, rsize=100)

    def headers(self, user=None):
        return {'HTTP_TOKEN': Auth.get_login_token(user or self.owner)['token']}

    def put(self, data):
        return self.client.put('/api/songs/' + str(self.song.uid), data,
                               content_type='application/json', **self.headers())

    def test_owner_and_scoped_anonymous_share(self):
        self.assertEqual(self.client.get('/api/songs/' + str(self.song.uid)).status_code, 401)
        self.assertEqual(self.client.get('/api/songs/' + str(self.song.uid), **self.headers(self.other)).status_code, 404)
        result = self.client.get('/api/song-shares/' + str(self.share.token)).json()['body']
        self.assertEqual(result['type'], 'song')
        self.assertIn('id', result['lyrics'])
        self.assertEqual(result['lyrics']['cues'], [{'time_ms': 1200, 'text': 'Hi'}])
        self.assertNotIn('disk/', str(result))
        for resource in (self.private, self.cover, self.root):
            self.assertEqual(self.client.get('/api/res/' + resource.res_str_id + '/content',
                {'share': str(self.share.token)}).status_code, 403)
        self.assertEqual(self.client.get('/api/shares/' + str(self.share.token)).json()['body']['type'], 'song')

    @patch('Resource.songs.qn_res_manager.get_resource_url', return_value='https://cdn.example/audio?e=123&token=signed')
    def test_playback_revocation_and_expiry(self, sign):
        url = '/api/song-shares/' + str(self.share.token) + '/playback'
        first = self.client.get(url)
        data = first.json()['body']
        self.assertEqual(data['expires_in'], 300)
        self.assertEqual(data['audio']['mime'], 'audio/mpeg')
        self.assertIn('no-store', first['Cache-Control'])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(sign.call_count, 2)
        self.assertEqual(self.client.delete('/api/shares/' + str(self.share.token), **self.headers(self.other)).status_code, 403)
        self.assertEqual(self.client.delete('/api/shares/' + str(self.share.token), **self.headers()).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(sign.call_count, 2)

    def test_versions_rename_move_replace_and_delete(self):
        self.audio.rname = 'Renamed.mp3'
        self.audio.parent = self.system
        self.audio.save()
        self.assertEqual(entry(self.audio)['song']['song_id'], str(self.song.uid))
        self.song.refresh_from_db()
        self.assertEqual(self.song.revision, 1)
        self.assertEqual(self.put({'version': 0, 'title': 'Stale'}).status_code, 409)
        self.assertEqual(self.put({'version': 1, 'title': 'Edited', 'cover_resource_id': 'cover'}).status_code, 200)
        self.song.refresh_from_db()
        self.assertEqual(self.song.revision, 2)
        self.audio.delete()
        self.song.refresh_from_db()
        self.assertIsNone(self.song.audio_id)
        self.assertEqual(self.song.revision, 3)
        self.assertEqual(self.client.get('/api/song-shares/' + str(self.share.token) + '/playback').status_code, 410)
        replacement = self.resource('replace', parent=self.root, mime='audio/mpeg')
        self.assertEqual(self.put({'version': 3, 'audio_resource_id': replacement.res_str_id}).status_code, 200)
        self.song.refresh_from_db()
        self.assertEqual(self.song.revision, 4)
        self.cover.delete()
        self.song.refresh_from_db()
        self.assertEqual(self.song.revision, 5)

    def test_input_permissions_and_plain_audio_not_song(self):
        other_audio = self.resource('not-song', parent=self.root, mime='audio/mpeg')
        self.assertNotIn('song', entry(other_audio))
        self.assertEqual(self.put({'version':1, 'lyrics':{'type':'lrc','text':'bad'}}).status_code, 400)
        self.assertEqual(self.put({'version':1, 'artists':'one'}).status_code, 400)
        self.assertEqual(self.put({'version':1, 'audio_resource_id':'private'}).status_code, 400)
        self.cover.owner = self.other
        self.cover.save()
        self.assertEqual(self.put({'version':1, 'cover_resource_id':'cover'}).status_code, 404)
        self.assertEqual(self.song.title, 'Test Song')

    def test_legacy_share_and_picker_share(self):
        legacy = Share.objects.create(resource=self.audio)
        self.assertIn('resource', self.client.get('/api/shares/' + str(legacy.token)).json()['body'])
        response = self.client.post('/api/shares/', {'res_str_id': self.audio.res_str_id},
                                    content_type='application/json', **self.headers())
        self.assertEqual(response.json()['body']['type'], 'song')
        self.assertEqual(self.client.get('/api/shares/', **self.headers()).status_code, 200)

    def test_create_from_metadata_and_picker_song_share(self):
        audio = self.resource('new-audio', parent=self.root, mime='audio/mpeg')
        AudioMetadata.objects.create(audio=audio, status='ready', data={
            'title':'Extracted', 'artists':['One','Two'], 'album':'Album', 'duration_ms':1000,
            'lyrics':lyrics_data('Plain lyrics')})
        created = self.client.post('/api/songs/', {'audio_resource_id':audio.res_str_id},
                                   content_type='application/json', **self.headers())
        self.assertEqual(created.status_code, 200, created.content)
        result = created.json()['body']
        self.assertEqual(result['title'], 'Extracted')
        self.assertEqual(result['artists'], ['One','Two'])
        self.assertEqual(result['lyrics']['text'], 'Plain lyrics')
        ticket = self.client.post('/api/picker/session', **self.headers()).json()['body']['ticket']
        picked = self.client.post('/api/picker/share', {'ticket':ticket,'resource_id':audio.res_str_id},
                                  content_type='application/json', **self.headers())
        self.assertEqual(picked.status_code,200,picked.content)
        self.assertEqual(picked.json()['body']['type'],'song')
        self.assertEqual(picked.json()['body']['song']['song_id'],result['song_id'])

    @patch('Resource.song_metadata.requests.get', side_effect=OSError('not available'))
    def test_extraction_failure_does_not_delete_original(self, fetch):
        metadata = AudioMetadata.objects.create(audio=self.audio)
        extract(metadata.pk)
        metadata.refresh_from_db()
        self.assertEqual(metadata.status, 'failed')
        self.assertTrue(Resource.objects.filter(pk=self.audio.pk).exists())

    @patch('Resource.song_signals.qn_res_manager.delete_res')
    def test_derived_artwork_cleanup(self, cleanup):
        metadata = AudioMetadata.objects.create(audio=self.audio)
        SongCover.objects.create(metadata=metadata, storage_key='disk/song-covers/test.jpg', mime='image/jpeg', ready=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.audio.delete()
        cleanup.assert_called_once_with('disk/song-covers/test.jpg', missing_ok=True)

    @patch('Resource.views.qn_res_manager.auth_callback')
    @patch('Resource.song_metadata.schedule', side_effect=OSError('metadata unavailable'))
    def test_callback_succeeds_if_scheduling_fails_and_is_idempotent(self, schedule, verify):
        payload = {'user_id':self.owner.pk, 'fname':'callback.mp3', 'ftype':'audio/mpeg',
                   'fsize':1200, 'key':'disk/res/callback.mp3'}
        url = '/api/res/' + self.root.res_str_id + '/token'
        first = self.client.post(url, payload, content_type='application/json')
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(url, payload, content_type='application/json')
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(Resource.objects.filter(dlpath=payload['key']).count(), 1)
        self.assertEqual(first.json()['body']['res_str_id'], second.json()['body']['res_str_id'])

    @patch('Resource.song_signals.qn_res_manager.delete_res', side_effect=OSError('CDN offline'))
    def test_cleanup_failure_keeps_durable_orphan_record(self, cleanup):
        metadata = AudioMetadata.objects.create(audio=self.audio)
        asset = SongCover.objects.create(metadata=metadata, storage_key='disk/song-covers/pending.jpg',
                                        mime='image/jpeg', ready=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.audio.delete()
        asset.refresh_from_db()
        self.assertIsNone(asset.metadata_id)



class MetadataTests(TestCase):
    def tags(self, version=4):
        tags = ID3()
        tags.add(TIT2(encoding=3, text=['歌名']))
        tags.add(TPE1(encoding=3, text=['歌手甲', '歌手乙']))
        tags.add(TALB(encoding=3, text=['专辑']))
        tags.add(USLT(encoding=3, lang='zho', text='普通歌词'))
        tags.add(SYLT(encoding=3, lang='zho', format=2, type=1, text=[('同步歌词', 1234)]))
        for kind, color in ((4, 'red'), (3, 'blue')):
            image = BytesIO()
            Image.new('RGB', (10,10), color).save(image, 'JPEG')
            tags.add(APIC(encoding=3, mime='image/jpeg', type=kind, desc=str(kind), data=image.getvalue()))
        stream = BytesIO()
        tags.save(stream, v2_version=version)
        return stream

    def test_id3_versions_front_cover_and_lyrics(self):
        for version in (3,4):
            data, cover = parse_mp3(self.tags(version))
            self.assertEqual(data['title'], '歌名')
            self.assertTrue(data['artists'])
            self.assertEqual(data['lyrics']['type'], 'synchronized')
            self.assertEqual(data['plain_lyrics'][0]['text'], '普通歌词')
            self.assertEqual(data['synchronized_lyrics'][0]['cues'][0]['time_ms'], 1234)
            pixel = Image.open(BytesIO(cover)).getpixel((1,1))
            self.assertGreater(pixel[2], pixel[0])

    def test_id3_v1_and_v22_and_duration(self):
        silent = (bytes.fromhex('fffb9000') + bytes(413)) * 200
        data, _ = parse_mp3(BytesIO(silent))
        self.assertGreater(data['duration_ms'], 4000)
        self.assertLess(data['duration_ms'], 6000)
        v1 = b'TAG' + b'Title'.ljust(30, b'\0') + b'Artist'.ljust(30, b'\0') + b'Album'.ljust(30, b'\0') + b'2026' + bytes(30) + b'\0'
        self.assertEqual(parse_mp3(BytesIO(silent + v1))[0]['title'], 'Title')
        title = b'\0Title 22'
        frame = b'TT2' + len(title).to_bytes(3, 'big') + title
        tag = b'ID3' + bytes([2,0,0,0,0,0,len(frame)]) + frame
        self.assertEqual(parse_mp3(BytesIO(tag + silent))[0]['title'], 'Title 22')

    def test_lrc_and_bad_audio(self):
        self.assertEqual(lyrics_data('[00:02][00:03.50]Hello','lrc')['cues'][1]['time_ms'],3500)
        self.assertEqual(parse_mp3(BytesIO(b'bad data'))[0]['duration_ms'], None)
