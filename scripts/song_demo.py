"""Local-only song fixture. Uses an isolated DB and simulated signed CDN, never production storage.

Run: python scripts/song_demo.py --directory /tmp/pocklo-song-demo
Frontend: build --configuration development first. Open http://localhost:8000/__song_demo
"""
import argparse
import hashlib
import hmac
import mimetypes
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlsplit

parser = argparse.ArgumentParser()
parser.add_argument('--directory', required=True)
args = parser.parse_args()
directory = Path(args.directory).resolve()
directory.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'disk.settings')
import disk.settings
disk.settings.PROJ_INIT = True
from django.conf import settings
settings.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(directory / 'demo.sqlite3')}}
settings.ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']
settings.SECRET_KEY = 'pocklo-local-song-demo-only'
settings.DEBUG = True
settings.POCKLO_OAUTH_WEB_ORIGIN = 'http://localhost:8000'
import django
django.setup()
from django.core.management import call_command
call_command('migrate', verbosity=0, interactive=False)
from django.utils import timezone
from Resource.models import Resource, Song, Share, AudioMetadata
from Resource.song_metadata import parse_mp3
from Resource.validators import RtypeChoice, StypeChoice
from User.models import User
from mutagen.id3 import ID3, TIT2, TPE1, TALB, USLT
import Base.jtoken as jtoken
jtoken.SECRET_KEY = 'pocklo-song-demo-signing-key-for-local-only'
jtoken.JWT_ENCODE_ALGO = 'HS256'

tags = ID3()
tags.add(TIT2(encoding=3, text=['小兜联调静音曲']))
tags.add(TPE1(encoding=3, text=['Pocklo Demo']))
tags.add(TALB(encoding=3, text=['本地联调']))
tags.add(USLT(encoding=3, lang='zho', text='[00:00.00]本地生成的静音样例\n[00:02.00]用于验证播放与拖动'))
output = BytesIO()
tags.save(output)
# Valid MPEG-1 Layer III frames, generated locally without copyrighted audio.
audio_data = output.getvalue() + (bytes.fromhex('fffb9000') + bytes(413)) * 800
(directory / 'demo.mp3').write_bytes(audio_data)
extracted, _ = parse_mp3(BytesIO(audio_data))
user, _ = User.objects.get_or_create(qt_user_app_id='pocklo-song-demo', defaults={'qtb_token':'', 'nickname':'歌曲联调'})
system, _ = Resource.objects.get_or_create(pk=1, defaults={
    'owner':user, 'parent':None, 'res_str_id':'sysdem', 'rname':'system',
    'rtype':RtypeChoice.FOLDER, 'create_time':timezone.now()})
root, _ = Resource.objects.get_or_create(res_str_id='demort', defaults={
    'owner':user, 'parent':system, 'rname':'歌曲联调目录',
    'rtype':RtypeChoice.FOLDER, 'create_time':timezone.now()})
audio, _ = Resource.objects.get_or_create(res_str_id='demomp', defaults={
    'owner':user, 'parent':root, 'rname':'demo.mp3', 'mime':'audio/mpeg',
    'rtype':RtypeChoice.FILE, 'sub_type':StypeChoice.MUSIC, 'create_time':timezone.now(),
    'rsize':len(audio_data), 'dlpath':'disk/demo/song.mp3'})
AudioMetadata.objects.update_or_create(audio=audio, defaults={'status':'ready', 'data':extracted})
song, _ = Song.objects.get_or_create(audio=audio, defaults={
    'owner':user, 'title':extracted['title'], 'artists':extracted['artists'], 'album':extracted['album'],
    'duration_ms':extracted['duration_ms'], 'lyrics':extracted['lyrics']})
share = Share.objects.filter(song=song).first() or Share.objects.create(song=song)

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
secret = b'pocklo-local-cdn-only'


def sign_url(key, expires=300, operation=None):
    # For local expiry recovery the URL expires after 4 seconds, without changing the production TTL.
    deadline = str(int(time.time()) + 4)
    signature = hmac.new(secret, deadline.encode(), hashlib.sha256).hexdigest()
    return f'http://localhost:8012/audio.mp3?e={deadline}&signature={signature}'


class CDN(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        deadline = query.get('e', ['0'])[0]
        signature = query.get('signature', [''])[0]
        valid = hmac.compare_digest(signature, hmac.new(secret, deadline.encode(), hashlib.sha256).hexdigest())
        if not valid or int(deadline) < time.time():
            self.send_response(403)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            return
        span = self.headers.get('Range')
        start, end = 0, len(audio_data) - 1
        if span:
            match = re.fullmatch(r'bytes=(\d+)-(\d*)', span)
            if not match:
                self.send_error(416)
                return
            start = int(match[1])
            end = min(int(match[2]), end) if match[2] else end
        if start > end:
            self.send_error(416)
            return
        self.send_response(206 if span else 200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Expose-Headers', 'Content-Range,Accept-Ranges,Content-Length')
        self.send_header('Cache-Control', 'private, no-store')
        if span:
            self.send_header('Content-Range', f'bytes {start}-{end}/{len(audio_data)}')
        self.end_headers()
        self.wfile.write(audio_data[start:end + 1])

    def log_message(self, *args):
        pass


Thread(target=ThreadingHTTPServer(('127.0.0.1',8012), CDN).serve_forever, daemon=True).start()
from Base.qn_manager import qn_res_manager
qn_res_manager.get_resource_url = sign_url
from django.http import HttpResponse
from django.urls import path, re_path
from Base.auth import Auth
from disk.urls import urlpatterns as api_urls
from Resource.songs import contract
import json
dist = Path(__file__).resolve().parents[2] / 'disk-frontend/dist/disk-frontend'
token = Auth.get_login_token(user)['token']


def login(request):
    return HttpResponse('<!doctype html><meta charset="utf-8"><button onclick="localStorage.setItem(\'token\',' +
        json.dumps(token).replace('"', '&quot;') + '); location.href=\'/res\';">登录本地联调账号</button>'
        f'<p><a href="/share/{share.token}">匿名歌曲分享</a></p>')


def frontend(request, filename=''):
    file = (dist / filename).resolve()
    if not file.is_relative_to(dist) or not file.is_file():
        file = dist / 'index.html'
    return HttpResponse(file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'text/html')


import types
urls = types.ModuleType('pocklo_song_demo_urls')
urls.urlpatterns = [path('__song_demo', login), *api_urls, re_path(r'^(?P<filename>.*)$', frontend)]
sys.modules[urls.__name__] = urls
settings.ROOT_URLCONF = urls.__name__
(directory / 'song.json').write_text(json.dumps(contract(song, share), ensure_ascii=False, indent=2))
print(f'Local song share: http://localhost:8000/share/{share.token}', flush=True)
print('Editor login: http://localhost:8000/__song_demo', flush=True)
call_command('runserver', '127.0.0.1:8000', use_reloader=False)
