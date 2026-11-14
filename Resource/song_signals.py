import logging

from django.db import transaction
from django.db.models import F, Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone

from Base.qn_manager import qn_res_manager
from Resource.models import AudioMetadata, Resource, Song, SongCover

logger = logging.getLogger(__name__)


def clean_cover(asset_id):
    asset = SongCover.objects.filter(pk=asset_id, metadata__isnull=True).first()
    if not asset:
        return
    try:
        qn_res_manager.delete_res(asset.storage_key, missing_ok=True)
    except Exception:
        logger.warning('Derived cover cleanup deferred for asset %s', asset_id)
        return
    asset.delete()


@receiver(pre_delete, sender=Resource)
def invalidate_song_resource(sender, instance, **kwargs):
    Song.objects.filter(Q(audio=instance) | Q(cover=instance)).update(
        revision=F('revision') + 1, updated_at=timezone.now())


@receiver(pre_delete, sender=AudioMetadata)
def defer_cover_cleanup(sender, instance, **kwargs):
    asset_id = SongCover.objects.filter(metadata=instance).values_list('pk', flat=True).first()
    if asset_id:
        transaction.on_commit(lambda: clean_cover(asset_id))
