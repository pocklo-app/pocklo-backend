""" Adel Liu 180111

资源类和方法
"""
import datetime
import uuid
from urllib.parse import urlencode

from diq import Dictify
from django.db import models
from django.utils.crypto import get_random_string
from smartdjango import Error

from Resource.validators import ResourceErrors, RtypeChoice, StypeChoice, CoverChoice, ResourceValidator
from User.models import User


class Resource(models.Model, Dictify):
    """
    资源类
    根资源文件夹id=1
    一旦新增用户，就在根目录创建一个属于新增用户的文件夹
    """
    ROOT_ID = 1
    FOLDER_MIME = 'inode/directory'
    LINK_MIME = 'text/uri-list'
    vldt = ResourceValidator

    rname = models.CharField(
        verbose_name='resource name',
        max_length=vldt.MAX_RNAME_LENGTH,
        validators=[vldt.rname]
    )
    rtype = models.IntegerField(
        verbose_name='file or folder',
        choices=RtypeChoice.to_choices(),
    )
    rsize = models.IntegerField(
        default=0,
    )
    sub_type = models.IntegerField(
        verbose_name='sub type',
        choices=StypeChoice.to_choices(),
        default=StypeChoice.FOLDER,
    )
    mime = models.CharField(
        verbose_name='资源类型',
        null=True,
        default=True,
        max_length=vldt.MAX_MIME_LENGTH,
    )
    cover = models.CharField(
        null=True,
        blank=True,
        default=None,
        max_length=vldt.MAX_COVER_LENGTH,
    )
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        'Resource',
        null=True,
        blank=True,
        default=0,
        on_delete=models.CASCADE,
        validators=[vldt.parent]
    )
    dlpath = models.CharField(
        verbose_name='download relative path to res.6-79.cn',
        max_length=vldt.MAX_DLPATH_LENGTH,
        default=None,
        null=True,
        blank=True,
    )
    create_time = models.DateTimeField()
    dlcount = models.IntegerField(
        verbose_name='download number',
        default=0,
    )
    res_str_id = models.CharField(
        verbose_name='唯一随机资源ID，弃用res_id',
        default=None,
        null=True,
        blank=True,
        max_length=vldt.MAX_RES_STR_ID_LENGTH,
        unique=True,
    )
    cover_type = models.IntegerField(
        choices=CoverChoice.to_choices(),
        verbose_name='封面类型 0 上传图片 1 与父资源相同 2 与指定资源相同 3 外部URI链接',
        default=CoverChoice.RANDOM,
        null=False,
        blank=False,
    )

    @classmethod
    def get_unique_id(cls):
        while True:
            res_str_id = get_random_string(length=6)
            try:
                cls.get_by_id(res_str_id)
            except Error as e:
                if e == ResourceErrors.RESOURCE_NOT_FOUND:
                    return res_str_id

    @classmethod
    def create_abstract(cls, rname, rtype, user, parent, dlpath, rsize, sub_type, mime):
        crt_time = datetime.datetime.now()

        return cls(
            rname=rname,
            rtype=rtype,
            mime=mime,
            cover=None,
            cover_type=CoverChoice.SELF if sub_type == StypeChoice.IMAGE else CoverChoice.RANDOM,
            owner=user,
            parent=parent,
            dlpath=dlpath,
            create_time=crt_time,
            rsize=rsize,
            sub_type=sub_type,
            res_str_id=cls.get_unique_id(),
            dlcount=0,
        )

    @classmethod
    def create_file(cls, rname, user, res_parent, dlpath, rsize, sub_type, mime):
        """ 创建文件对象

        :param mime: 七牛返回的资源类型
        :param rname: 文件名
        :param user: 所属用户
        :param res_parent: 所属目录
        :param dlpath: 七牛存储的key
        :param rsize: 文件大小
        :param sub_type: 文件分类
        :return: Ret对象，错误返回错误代码，成功返回文件对象
        """
        try:
            res = cls.create_abstract(
                rname=rname,
                rtype=RtypeChoice.FILE,
                user=user,
                parent=res_parent,
                dlpath=dlpath,
                rsize=rsize,
                sub_type=sub_type,
                mime=mime,
            )
            res.save()
        except Exception:
            raise ResourceErrors.CREATE_FILE
        return res

    @classmethod
    def create_folder(cls, rname, user, res_parent):
        """ 创建文件夹对象

        :param rname: 文件夹名
        :param user: 所属用户
        :param res_parent: 所属目录
        :return: Ret对象，错误返回错误代码，成功返回文件夹对象
        """
        try:
            res = cls.create_abstract(
                rname=rname,
                rtype=RtypeChoice.FOLDER,
                user=user,
                parent=res_parent,
                dlpath=None,
                rsize=0,
                sub_type=StypeChoice.FOLDER,
                mime=cls.FOLDER_MIME,
            )
            res.save()
        except Exception as err:
            raise ResourceErrors.CREATE_FOLDER(details=err)
        return res

    @classmethod
    def create_link(cls, rname, user, res_parent, dlpath):
        """ 创建链接对象

        :param rname: 链接名称
        :param user: 所属用户
        :param res_parent: 所在目录
        :param dlpath: 链接地址
        :return: Ret对象，错误返回错误代码，成功返回链接对象
        """
        try:
            res = cls.create_abstract(
                rname=rname,
                rtype=RtypeChoice.LINK,
                user=user,
                parent=res_parent,
                dlpath=dlpath,
                rsize=0,
                sub_type=StypeChoice.LINK,
                mime=cls.LINK_MIME,
            )
            res.save()
        except Exception as err:
            raise ResourceErrors.CREATE_LINK(details=err)
        return res

    """
    查询方法
    """

    @classmethod
    def get_by_id(cls, res_str_id):
        try:
            res = cls.objects.get(res_str_id=res_str_id)
        except cls.DoesNotExist:
            raise ResourceErrors.RESOURCE_NOT_FOUND
        return res

    @classmethod
    def get_by_pk(cls, res_id):
        """根据资源id获取资源对象"""
        try:
            res = cls.objects.get(pk=res_id)
        except cls.DoesNotExist:
            raise ResourceErrors.RESOURCE_NOT_FOUND
        return res

    def belong(self, user):
        """判断资源是否属于用户"""
        return self.owner.pk == user.pk

    def is_home(self):
        return self.parent_id == Resource.ROOT_ID

    def get_cover_urls(self):
        """Only media can have a cover, always derived from its own object."""
        if self.rtype != RtypeChoice.FILE or not self.dlpath:
            return None, None
        operation = {
            StypeChoice.IMAGE: 'imageView2/0/w/480',
            StypeChoice.VIDEO: 'vframe/jpg/offset/0/w/480/rotate/auto',
        }.get(self.sub_type)
        if not operation:
            return None, None
        from Base.qn_manager import qn_res_manager
        thumbnail = qn_res_manager.get_resource_url(self.dlpath, expires=300, operation=operation)
        return thumbnail, thumbnail

    """
    字典方法
    """

    def _dictify_owner(self):
        return self.owner.d()

    def _dictify_create_time(self):
        return self.create_time.timestamp()

    def _dictify_is_home(self):
        return self.is_home()

    def _dictify_parent_str_id(self):
        return self.parent.res_str_id if self.parent_id else None

    def d(self):
        dict_ = self.dictify('res_str_id', 'rname', 'rtype', 'rsize', 'sub_type',
                             'owner', 'parent_str_id', 'create_time', 'dlcount', 'is_home')
        dict_['status'] = 1  # Fixed compatibility value for released CLI versions.
        dict_['cover_type'] = (
            CoverChoice.SELF if self.sub_type in (StypeChoice.IMAGE, StypeChoice.VIDEO) else CoverChoice.RANDOM
        )
        cover_urls = self.get_cover_urls()
        dict_.update(dict(
            cover=cover_urls[0],
            cover_small=cover_urls[1],
        ))
        return dict_

    def d_base(self, readable=False):
        dict_ = self.dictify('is_home', 'owner', 'create_time')
        cover_urls = self.get_cover_urls() if readable else (None, None)
        dict_.update(dict(
            cover=cover_urls[0],
            cover_small=cover_urls[1],
        ))
        return dict_

    def d_child(self):
        dict_ = self.dictify(
            'res_str_id',
            'rname',
            'rtype',
            'rsize',
            'create_time',
            'sub_type',
            'dlcount',
        )
        cover_urls = self.get_cover_urls()
        dict_.update(dict(
            cover_small=cover_urls[1],
            status=1,  # Compatibility with the released CLI listing.
        ))
        return dict_

    def d_layer(self):
        children = Resource.objects.filter(parent=self, owner_id=self.owner_id)
        return dict(info=self.d(), child_list=[child.d_child() for child in children])

    def d_child_selector(self):
        return self.dictify('res_str_id', 'rname', 'rtype', 'sub_type')

    def d_selector_layer(self):
        child_list = Resource.objects.filter(parent=self)
        child_list = [child.d_child_selector() for child in child_list]
        info = self.dictify('is_home', 'res_str_id', 'rname', 'parent_str_id')
        return dict(
            info=info,
            child_list=child_list,
        )

    """
    查询方法
    """

    @classmethod
    def get_root_folder(cls, user):
        """获取当前用户的根目录"""
        try:
            res = cls.objects.get(owner=user, parent=1, rtype=RtypeChoice.FOLDER)
        except cls.DoesNotExist:
            raise ResourceErrors.GET_ROOT_FOLDER
        return res

    def get_dl_url(self, expires=3600, attachment=False):
        """获取当前资源的下载链接"""
        Resource.objects.filter(pk=self.pk).update(dlcount=models.F('dlcount') + 1)
        if self.rtype == RtypeChoice.LINK:
            return self.dlpath
        from Base.qn_manager import qn_res_manager
        operation = urlencode({'attname': self.rname}) if attachment else None
        return qn_res_manager.get_resource_url(self.dlpath, expires=expires, operation=operation)

    def is_empty(self):
        """ 资源是否为空（针对文件夹资源） """
        res_list = Resource.objects.filter(parent=self)
        if res_list:
            return False
        return True

    def remove(self):
        """ 删除资源 """
        if self.rtype == RtypeChoice.FOLDER:
            if not self.is_empty():
                raise ResourceErrors.REQUIRE_EMPTY_FOLDER
        from Base.qn_manager import qn_res_manager
        if self.rtype == RtypeChoice.FILE:
            qn_res_manager.delete_res(self.dlpath)
        self.delete()


class Share(models.Model):
    """A revocable link scoped to one resource and its current descendants."""

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    resource = models.ForeignKey(Resource, null=True, on_delete=models.CASCADE, related_name='shares')
    song = models.ForeignKey('Song', null=True, blank=True, on_delete=models.CASCADE, related_name='shares')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(
            condition=(models.Q(resource__isnull=False, song__isnull=True) |
                       models.Q(resource__isnull=True, song__isnull=False)),
            name='share_exactly_one_target')]


class AudioMetadata(models.Model):
    """Extraction is a suggestion, never an automatic song classification."""
    audio = models.OneToOneField(Resource, on_delete=models.CASCADE, related_name='audio_metadata')
    status = models.CharField(max_length=16, default='pending')
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class Song(models.Model):
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE)
    audio = models.OneToOneField(Resource, null=True, on_delete=models.SET_NULL, related_name='song')
    revision = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=256)
    artists = models.JSONField(default=list)
    album = models.CharField(max_length=256, blank=True)
    duration_ms = models.PositiveIntegerField(null=True)
    # A user-selected image remains a normal private resource.
    cover = models.ForeignKey(Resource, null=True, blank=True, on_delete=models.SET_NULL, related_name='song_covers')
    use_embedded_cover = models.BooleanField(default=True)
    lyrics_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    lyrics = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class SongCover(models.Model):
    """Derived artwork in the existing private disk/ namespace."""
    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    metadata = models.OneToOneField(AudioMetadata, null=True, on_delete=models.SET_NULL, related_name='artwork')
    storage_key = models.CharField(max_length=256, unique=True)
    mime = models.CharField(max_length=32)
    ready = models.BooleanField(default=False)
