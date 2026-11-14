from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from Resource.models import AudioMetadata
from Resource.song_metadata import extract


class Command(BaseCommand):
    help = 'Process pending/stale audio metadata jobs, e.g. once a minute from the service scheduler.'

    def handle(self, *args, **options):
        ids = AudioMetadata.objects.filter(
            Q(status='pending') | Q(status='parsing', updated_at__lt=timezone.now() - timedelta(minutes=2))
        ).values_list('pk', flat=True)[:100]
        for metadata_id in list(ids):
            extract(metadata_id)
