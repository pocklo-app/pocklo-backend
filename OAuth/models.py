import uuid

from django.db import models


class Application(models.Model):
    client_id = models.CharField(max_length=48, primary_key=True)
    developer = models.ForeignKey('User.User', on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    website = models.URLField(max_length=500)
    purpose = models.CharField(max_length=500)
    redirect_uris = models.JSONField()
    secret_hash = models.CharField(max_length=64)
    review = models.CharField(max_length=12, default='none')
    review_reason = models.CharField(max_length=500, blank=True)
    enabled = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)


class Grant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    user = models.ForeignKey('User.User', on_delete=models.CASCADE)
    scope = models.CharField(max_length=80)
    revision = models.PositiveIntegerField()
    generation = models.PositiveIntegerField(default=1)
    revoked = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['application', 'user'], name='oauth_app_user_grant'),
        ]


class AuthorizationRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    application = models.ForeignKey(Application, on_delete=models.CASCADE)
    user = models.ForeignKey('User.User', null=True, on_delete=models.CASCADE)
    redirect_uri = models.URLField(max_length=500)
    scope = models.CharField(max_length=80)
    revision = models.PositiveIntegerField()
    state = models.CharField(max_length=256)
    challenge = models.CharField(max_length=43)
    expires_at = models.DateTimeField(db_index=True)


class Credential(models.Model):
    digest = models.CharField(max_length=64, primary_key=True)
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE)
    kind = models.CharField(max_length=8)
    generation = models.PositiveIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    used = models.BooleanField(default=False)
    redirect_uri = models.URLField(max_length=500, blank=True)
    challenge = models.CharField(max_length=43, blank=True)


class ShareCreation(models.Model):
    grant = models.ForeignKey(Grant, on_delete=models.CASCADE)
    key_hash = models.CharField(max_length=64)
    resource_id_at_creation = models.CharField(max_length=64)
    share = models.ForeignKey('Resource.Share', null=True, on_delete=models.SET_NULL)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['grant', 'key_hash'], name='oauth_share_idempotency_key'),
        ]
