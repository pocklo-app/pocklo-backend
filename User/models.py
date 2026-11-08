""" Adel Liu 180111

用户类
"""
import logging
import time

from diq import Dictify
from django.db import models
from smartdjango import Error

from Base.common import qt_manager
from User.validators import UserValidator, UserErrors


logger = logging.getLogger(__name__)


class User(models.Model, Dictify):
    vldt = UserValidator

    ROOT_ID = 1
    PROFILE_SYNC_TTL = 10 * 60

    avatar = models.CharField(
        default=None,
        null=True,
        blank=True,
        max_length=vldt.MAX_AVATAR_LENGTH,
    )

    nickname = models.CharField(
        max_length=vldt.MAX_NICKNAME_LENGTH,
        default=None,
        blank=True,
        null=True,
    )

    qt_user_app_id = models.CharField(
        default=None,
        max_length=vldt.MAX_QT_USER_APP_ID_LENGTH,
        unique=True,
        null=True,
    )

    qtb_token = models.CharField(
        default=None,
        max_length=vldt.MAX_QTB_TOKEN_LENGTH,
    )

    description = models.CharField(
        max_length=vldt.MAX_DESCRIPTION_LENGTH,
        default=None,
        blank=True,
        null=True,
    )

    profile_sync_time = models.FloatField(
        default=0,
        blank=True,
        null=True,
    )

    """
    查询函数
    """

    @staticmethod
    def get_by_id(user_id):
        """根据用户ID获取用户对象"""
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            raise UserErrors.USER_NOT_FOUND
        return user

    @staticmethod
    def get_by_qtid(qt_user_app_id):
        """根据齐天用户-应用ID获取用户对象"""
        try:
            user = User.objects.get(qt_user_app_id=qt_user_app_id)
        except User.DoesNotExist:
            raise UserErrors.USER_NOT_FOUND
        return user

    """
    字典函数
    """

    def _dictify_user_id(self):
        return self.pk

    def _dictify_root_res(self):
        from Resource.models import Resource
        try:
            res = Resource.get_root_folder(self)
            return res.res_str_id
        except Exception:
            return None

    def d(self):
        return self.dictify('user_id', 'avatar', 'nickname', 'root_res')

    """
    增删函数
    """

    @classmethod
    def create(cls, qt_user_app_id, token):
        try:
            user = cls.get_by_qtid(qt_user_app_id)
            user.qtb_token = token
            user.save()
        except Error as e:
            if e == UserErrors.USER_NOT_FOUND:
                try:
                    user = cls(
                        qt_user_app_id=qt_user_app_id,
                        qtb_token=token,
                    )
                    user.save()
                except Exception:
                    return UserErrors.CREATE_USER
            else:
                return e
        return user

    def has_profile_cache(self):
        return any([
            self.avatar,
            self.nickname,
            self.description,
            self.profile_sync_time,
        ])

    def profile_is_fresh(self, now=None):
        if not self.profile_sync_time:
            return False
        if now is None:
            now = time.time()
        return now - self.profile_sync_time < self.PROFILE_SYNC_TTL

    def refresh_profile(self, force=False, suppress_error=False):
        now = time.time()
        if not self.qtb_token or (not force and self.profile_is_fresh(now)):
            return False

        try:
            body = qt_manager.get_user_info(self.qtb_token)
        except Error as err:
            if suppress_error:
                logger.warning(
                    'User profile refresh degraded user_id=%s qt_user_app_id=%s force=%s identifier=%s append_msg=%s',
                    self.pk,
                    self.qt_user_app_id,
                    force,
                    err.identifier,
                    getattr(err, 'append_msg', ''),
                )
                return False
            raise

        self.avatar = body.get('avatar')
        self.nickname = body.get('nickname')
        self.description = body.get('description')
        self.profile_sync_time = now
        self.save(update_fields=['avatar', 'nickname', 'description', 'profile_sync_time'])
        return True

    def update(self):
        return self.refresh_profile(force=True)

    def remove(self):
        return self.delete()


class FriendenBinding(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='frienden_binding')
    subject = models.CharField(max_length=255, unique=True)
    phone = models.CharField(max_length=14, unique=True)


class FriendenBindingFlow(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    state_hash = models.CharField(max_length=64)
    verifier = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    claimed = models.BooleanField(default=False)


class FriendenLoginFlow(models.Model):
    state_hash = models.CharField(max_length=64, primary_key=True)
    browser_hash = models.CharField(max_length=64)
    verifier = models.CharField(max_length=128)
    expires_at = models.DateTimeField(db_index=True)
