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

from Resource.validators import ResourceErrors, RtypeChoice, StypeChoice, StatusChoice, CoverChoice, ResourceValidator
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
    description = models.TextField(
        verbose_name='resource note',
        null=True,
        blank=True,
        default=None,
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
    status = models.IntegerField(
        choices=StatusChoice.to_choices(),
        verbose_name='加密状态 0公开 1仅自己可见 2需要密码',
        default=StatusChoice.PRIVATE,
    )
    visit_key = models.CharField(
        max_length=vldt.MAX_VISIT_KEY_LENGTH,
        verbose_name='当status为2时有效',
        validators=[vldt.visit_key],
    )
    vk_change_time = models.FloatField(
        null=True,
        blank=True,
        default=0,
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
    right_bubble = models.BooleanField(
        verbose_name='读取权限向上冒泡',
        default=False,
        null=True,
        blank=True,
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
    def create_abstract(cls, rname, rtype, desc, user, parent, dlpath, rsize, sub_type, mime):
        crt_time = datetime.datetime.now()

        return cls(
            rname=rname,
            rtype=rtype,
            mime=mime,
            description=desc,
            cover=None,
            cover_type=CoverChoice.SELF if sub_type == StypeChoice.IMAGE else CoverChoice.RANDOM,
            owner=user,
            parent=parent,
            dlpath=dlpath,
            status=StatusChoice.PRIVATE,
            visit_key=get_random_string(length=4),
            create_time=crt_time,
            vk_change_time=crt_time.timestamp(),
            rsize=rsize,
            sub_type=sub_type,
            res_str_id=cls.get_unique_id(),
            dlcount=0,
            right_bubble=False,
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
                desc=None,
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
    def create_folder(cls, rname, user, res_parent, desc=None):
        """ 创建文件夹对象

        :param rname: 文件夹名
        :param user: 所属用户
        :param res_parent: 所属目录
        :param desc: 描述说明
        :return: Ret对象，错误返回错误代码，成功返回文件夹对象
        """
        try:
            res = cls.create_abstract(
                rname=rname,
                rtype=RtypeChoice.FOLDER,
                desc=desc,
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
                desc=None,
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

    def _dictify_visit_key(self):
        return self.visit_key if self.status == StatusChoice.PROTECT else None

    def _dictify_is_home(self):
        return self.is_home()

    def _dictify_secure_env(self):
        if self.pk == Resource.ROOT_ID or \
                not self.right_bubble or \
                self.status == StatusChoice.PUBLIC:
            return True
        res = self.parent
        visited = {self.pk}
        while res and res.pk != Resource.ROOT_ID and res.pk not in visited:
            visited.add(res.pk)
            if res.status == StatusChoice.PUBLIC:
                return res.rname
            if not res.right_bubble:
                break
            res = res.parent
        return True

    def _dictify_parent_str_id(self):
        return self.parent.res_str_id if self.parent_id else None

    def d(self, include_key=False):
        dict_ = self.dictify('res_str_id', 'rname', 'rtype', 'rsize', 'sub_type', 'description',
                             'cover_type', 'owner', 'parent_str_id', 'status', 'create_time',
                             'dlcount', 'is_home', 'right_bubble', 'secure_env')
        if include_key:
            dict_['visit_key'] = self._dictify_visit_key()
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
        dict_ = self.dictify('status', 'is_home', 'owner', 'create_time', 'right_bubble')
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
            'status',
            'create_time',
            'sub_type',
            'dlcount',
        )
        cover_urls = self.get_cover_urls()
        dict_.update(dict(
            cover_small=cover_urls[1],
        ))
        return dict_

    def d_layer(self, user=None, visit_key=None):
        child_list = Resource.objects.filter(parent=self, owner_id=self.owner_id)
        child_list = [
            child.d_child() for child in child_list
            if child.readable(user, None)
            or (visit_key and self.status == StatusChoice.PROTECT and self.visit_key == visit_key
                and child.right_bubble)
        ]
        return dict(
            info=self.d(include_key=self.owner_id == getattr(user, 'pk', None)),
            child_list=child_list,
        )

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

    def readable(self, user, visit_key):
        """判断当前资源是否被当前用户可读"""
        res = self
        visited = set()
        while res and res.pk != Resource.ROOT_ID and res.pk not in visited:
            visited.add(res.pk)
            if res.owner_id == getattr(user, 'pk', None) or res.status == StatusChoice.PUBLIC:
                return True
            if res.status == StatusChoice.PROTECT and res.visit_key == visit_key:
                if user:
                    UserRight.update(user, res)
                return True
            if res.status == StatusChoice.PROTECT and UserRight.verify(user, res):
                return True
            if not res.right_bubble:
                break
            res = res.parent
            visit_key = None
        return False

    def get_dl_url(self, expires=3600, attachment=False):
        """获取当前资源的下载链接"""
        Resource.objects.filter(pk=self.pk).update(dlcount=models.F('dlcount') + 1)
        if self.rtype == RtypeChoice.LINK:
            return self.dlpath
        from Base.qn_manager import qn_res_manager
        operation = urlencode({'attname': self.rname}) if attachment else None
        return qn_res_manager.get_resource_url(self.dlpath, expires=expires, operation=operation)

    def get_visit_key(self):
        """获取当前资源的访问密码"""
        if self.status == StatusChoice.PROTECT:
            return self.visit_key
        return None

    """
    修改方法
    """

    def modify_info(self, rname, description, status, visit_key, right_bubble, parent):
        """ 修改资源属性

        :param rname: 资源名称
        :param description: 资源介绍
        :param status: 资源分享类型（公开、私有、加密）
        :param visit_key: 资源加密密钥
        :param right_bubble: 资源读取权限是否向上查询
        :param parent: 移动后的新父目录
        :return: Ret对象，错误返回错误代码，成功返回资源对象
        """
        if rname is None:
            rname = self.rname
        if description is None:
            description = self.description or ''
        if status is None:
            status = self.status
        if visit_key is None:
            visit_key = self.visit_key
        if right_bubble is None:
            right_bubble = self.right_bubble
        if parent is None:
            parent = self.parent

        self.rname = rname
        self.description = description
        self.status = status
        self.right_bubble = right_bubble
        self.parent = parent
        if status == StatusChoice.PROTECT:
            if self.visit_key != visit_key:
                self.visit_key = visit_key
                self.vk_change_time = datetime.datetime.now().timestamp()
        self.save(update_fields=['rname', 'description', 'status', 'right_bubble', 'parent',
                                 'visit_key', 'vk_change_time'])

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


class UserRight(models.Model):
    """
    用户可读资源权限表（记录加密资源可读性）
    """
    user = models.ForeignKey(
        'User.User',
        on_delete=models.CASCADE,
    )
    res = models.ForeignKey(
        'Resource.Resource',
        on_delete=models.CASCADE,
    )
    verify_time = models.FloatField(
        default=0,
    )

    @classmethod
    def update(cls, user: User, res: Resource):
        try:
            right = cls.get_right(user, res)
            right.verify_time = datetime.datetime.now().timestamp()
            right.save()
        except Error as e:
            if e == ResourceErrors.RIGHT_NOT_FOUND:
                try:
                    right = cls(
                        user=user,
                        res=res,
                        verify_time=datetime.datetime.now().timestamp()
                    )
                    right.save()
                except Exception:
                    raise ResourceErrors.CREATE_RIGHT
            else:
                return e
        return right

    @classmethod
    def get_right(cls, user, res):
        try:
            right = cls.objects.get(user=user, res=res)
        except cls.DoesNotExist:
            raise ResourceErrors.RIGHT_NOT_FOUND
        return right

    @classmethod
    def verify(cls, user: User, res: Resource):
        if not user:
            return False
        try:
            right = cls.get_right(user, res)
        except Error:
            return False
        if right.verify_time > (res.vk_change_time or 0):
            return True
        else:
            right.delete()
            return False


class Share(models.Model):
    """A revocable link scoped to one resource and its current descendants."""

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    resource = models.ForeignKey(Resource, on_delete=models.CASCADE, related_name='shares')
    created_at = models.DateTimeField(auto_now_add=True)
