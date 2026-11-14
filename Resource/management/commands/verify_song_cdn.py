"""Read-only deployment check, does not upload or make an object public."""
import requests
from django.core.management.base import BaseCommand, CommandError

from Resource.models import Share
from Resource.songs import TTL
from Base.qn_manager import qn_res_manager


class Command(BaseCommand):
    help = 'Verify an existing song CDN object supports audio MIME, ranges and the consumer origin.'

    def add_arguments(self, parser):
        parser.add_argument('--token', required=True)
        parser.add_argument('--origin', required=True)

    def handle(self, *args, **options):
        share = Share.objects.select_related('song__audio').filter(token=options['token'], song__isnull=False).first()
        if not share or not share.song.audio_id:
            raise CommandError('Song share unavailable')
        audio = share.song.audio
        url = qn_res_manager.get_resource_url(audio.dlpath, expires=TTL)
        with requests.get(url, headers={'Range':'bytes=0-1023', 'Origin':options['origin'],
                          'Accept-Encoding':'identity'}, timeout=(3,10), stream=True, allow_redirects=False) as result:
            if result.status_code != 206:
                raise CommandError('CDN must return 206 for a byte range')
            if not result.headers.get('Content-Type','').lower().startswith('audio/'):
                raise CommandError('CDN Content-Type is not audio')
            if not result.headers.get('Content-Range','').startswith('bytes 0-'):
                raise CommandError('CDN Content-Range missing')
            if result.headers.get('Access-Control-Allow-Origin') not in ('*', options['origin']):
                raise CommandError('Consumer origin is not allowed by CDN CORS')
        self.stdout.write(self.style.SUCCESS('CDN MIME, 206, Content-Range and CORS verified. Signed URL omitted.'))
