from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils.crypto import get_random_string
from django.views import View
from smartdjango import Validator, analyse, Key, OK
from smartdjango.analyse import Request

from Base.auth import Auth
from Base.policy import Policy
from Base.qn_manager import qn_res_manager, QnManager
from Resource.models import Resource, RtypeChoice, StypeChoice
from Resource.params import ResourceParams
from Resource.validators import ResourceErrors
from Resource.workspace import path_to
from User.models import User
from User.params import UserParams


class BaseView(View):
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def get(self, request: Request, **kwargs):
        """ GET /api/res/:res_str_id

        获取资源信息
        """
        return request.argument.resource.d_layer()

    @analyse.json(
        ResourceParams.rname.copy().null().default(None),
        ResourceParams.parent_getter.copy().null().default(None),
        restrict_keys=False,
    )
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    @transaction.atomic
    def put(self, request: Request, **kwargs):
        """ PUT /api/res/:slug/

        修改资源信息
        """
        user = request.user
        rname = request.json.rname
        parent: Resource = request.json.parent

        # Serialize tree edits by owner so two concurrent moves cannot create a cycle.
        User.objects.select_for_update().get(pk=user.pk)
        resource = Resource.get_by_id(request.argument.resource.res_str_id)

        if parent:
            parent = Resource.get_by_id(parent.res_str_id)
            if not parent.belong(user):
                raise ResourceErrors.RESOURCE_NOT_BELONG

            if parent.rtype != RtypeChoice.FOLDER:
                raise ResourceErrors.REQUIRE_FOLDER

            temp_res = parent
            visited = {resource.pk}
            while temp_res and temp_res.pk != Resource.ROOT_ID:
                if temp_res.pk in visited:
                    raise ResourceErrors.RESOURCE_CIRCLE
                visited.add(temp_res.pk)
                temp_res = temp_res.parent

        if rname is not None:
            resource.rname = rname
        if parent is not None:
            resource.parent = parent
        resource.save(update_fields=['rname', 'parent'])
        return resource.d()

    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    @transaction.atomic
    def delete(self, request: Request, **kwargs):
        """ DELETE /api/res/:slug

        删除资源
        """
        User.objects.select_for_update().get(pk=request.user.pk)
        resource = Resource.get_by_id(request.argument.resource.res_str_id)
        within = request.GET.get('within')
        if within:
            root = Resource.get_by_id(within)
            if not root.belong(request.user) or not path_to(resource, root):
                raise ResourceErrors.DELETE_OUTSIDE_DIRECTORY

        if resource.pk == Resource.ROOT_ID or resource.parent_id == Resource.ROOT_ID:
            raise ResourceErrors.DELETE_ROOT_FOLDER

        resource.remove()

        return OK


class FolderView(View):
    @analyse.json(ResourceParams.rname.copy().rename(Key('folder_name')))
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def post(self, request: Request, **kwargs):
        """ POST /api/res/:res_str_id/folder

        上传文件夹资源
        """
        user = request.user
        folder_name = request.json.folder_name
        res_parent: Resource = request.argument.resource

        if res_parent.rtype != RtypeChoice.FOLDER:
            raise ResourceErrors.REQUIRE_FOLDER

        res = Resource.create_folder(folder_name, user, res_parent)
        return res.d()


class LinkView(View):
    @analyse.json(ResourceParams.rname.copy().rename(Key('link_name')), 'link')
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def post(self, request: Request, **kwargs):
        """ POST /api/res/:res_str_id/link

        上传链接资源
        """
        user = request.user
        link_name = request.json.link_name
        link = request.json.link
        parent: Resource = request.argument.resource

        if parent.rtype != RtypeChoice.FOLDER:
            raise ResourceErrors.REQUIRE_FOLDER

        resource: Resource = Resource.create_link(link_name, user, parent, link)
        return resource.d()


class PathView(View):
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def get(self, request: Request, **kwargs):
        resource: Resource = request.argument.resource
        res_path = []
        while resource.pk != Resource.ROOT_ID:
            if resource.res_str_id in res_path:
                raise ResourceErrors.RESOURCE_CIRCLE
            res_path.append(resource.res_str_id)
            resource = resource.parent
        return res_path


class SelectView(View):
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def get(self, request: Request, **kwargs):
        resource: Resource = request.argument.resource
        return resource.d_selector_layer()


class TokenView(View):
    @analyse.query(ResourceParams.rname.copy().rename(Key('filename')))
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def get(self, request: Request, **kwargs):
        """ GET /api/res/:res_str_id/token

        获取七牛上传资源token
        """
        user = request.user

        filename = request.query.filename
        filename = QnManager.encode_key(filename)
        parent: Resource = request.argument.resource
        if parent.rtype != RtypeChoice.FOLDER:
            raise ResourceErrors.REQUIRE_FOLDER

        import datetime
        crt_time = datetime.datetime.now().timestamp()
        salt = get_random_string(4)
        key = 'res/%s/%s/%s' % (salt, crt_time, filename)
        qn_token, key = qn_res_manager.get_upload_token(
            key, Policy.file(filename, user.pk, parent.res_str_id))
        return dict(upload_token=qn_token, key=key)

    @analyse.json(UserParams.user_getter, 'fsize', 'fname', 'ftype', 'key')
    @analyse.argument(ResourceParams.resource_getter)
    def post(self, request: Request, **kwargs):
        """ POST /api/res/:res_str_id/token

        七牛上传资源回调函数
        """
        qn_res_manager.auth_callback(request)

        key = request.json.key
        user: User = request.json.user
        fsize = request.json.fsize
        fname = request.json.fname
        ftype = request.json.ftype
        parent: Resource = request.argument.resource

        if ftype.find('video') == 0:
            sub_type = StypeChoice.VIDEO
        elif ftype.find('image') == 0:
            sub_type = StypeChoice.IMAGE
        elif ftype.find('audio') == 0:
            sub_type = StypeChoice.MUSIC
        else:
            sub_type = StypeChoice.FILE

        if not parent.belong(user):
            raise ResourceErrors.PARENT_NOT_BELONG
        if parent.rtype != RtypeChoice.FOLDER:
            raise ResourceErrors.REQUIRE_FOLDER

        decode_fname = QnManager.decode_key(fname)
        if fname != decode_fname:
            new_key = '%s/%s' % (key[:key.rfind('/')], decode_fname)
            qn_res_manager.move_res(key, new_key)
            fname = decode_fname
            key = new_key

        resource = Resource.create_file(fname, user, parent, key, fsize, sub_type, ftype)
        return resource.d_child()


class BaseInfoView(View):
    @analyse.argument(ResourceParams.resource_getter)
    @Auth.require_owner
    def get(self, request, **kwargs):
        return dict(info=request.argument.resource.d_base(readable=True), readable=True)


class DownloadView(View):
    @analyse.query(Validator('token', '登录口令').null().default(None, as_final=True), restrict_keys=False)
    @analyse.argument(ResourceParams.resource_getter)
    def get(self, request, **kwargs):
        if request.query.token and not request.META.get('HTTP_TOKEN'):
            request.META['HTTP_TOKEN'] = request.query.token
        Auth._extract_user(request)
        resource = request.argument.resource
        if not resource.belong(request.user):
            raise ResourceErrors.NOT_READABLE
        if resource.rtype == RtypeChoice.FOLDER:
            raise ResourceErrors.REQUIRE_FILE
        return HttpResponseRedirect(resource.get_dl_url())
