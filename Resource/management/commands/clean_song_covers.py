from django.core.management.base import BaseCommand

from Resource.models import SongCover
from Resource.song_signals import clean_cover


class Command(BaseCommand):
    help = 'Retry deletion of derived covers whose original audio was deleted.'

    def handle(self, *args, **options):
        ids = list(SongCover.objects.filter(metadata__isnull=True).values_list('pk', flat=True)[:100])
        for asset_id in ids:
            clean_cover(asset_id)
