from django.core import signing
from django.views import View
from smartdjango import Code, Error, analyse

from Base.auth import Auth
from Resource.models import Resource, Share
from Resource.validators import ResourceErrors, RtypeChoice
from Resource.workspace import WorkspaceAccess, path_to, reply, share_info, workspace
from User.models import User


@Error.register
class PickerErrors:
    INVALID_SESSION = Error('资源选择会话已失效，请重新连接小兜', code=Code.Unauthorized)


TICKET_SALT = 'pocklo.picker.browse'
TICKET_AGE = 900


def ticket_user(value):
    try:
        return signing.loads(value, salt=TICKET_SALT, max_age=TICKET_AGE)
    except signing.BadSignature:
        raise PickerErrors.INVALID_SESSION


def picker_resource(user, resource_id=None):
    root = Resource.get_root_folder(user)
    if not resource_id:
        return root
    resource = Resource.get_by_id(resource_id)
    if not path_to(resource, root):
        raise ResourceErrors.NOT_READABLE
    return resource


class PickerSession(View):
    @Auth.require_login
    def post(self, request):
        return reply({'ticket': signing.dumps(request.user.pk, salt=TICKET_SALT)})


class PickerFiles(View):
    def get(self, request):
        scheme, _, ticket = request.headers.get('Authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not ticket:
            raise PickerErrors.INVALID_SESSION
        user = User.get_by_id(ticket_user(ticket))
        resource = picker_resource(user, request.GET.get('resource'))
        if resource.rtype != RtypeChoice.FOLDER:
            raise ResourceErrors.REQUIRE_FOLDER
        request.user = user
        return workspace(request, resource, WorkspaceAccess(request))


class PickerShare(View):
    @Auth.require_login
    @analyse.json('ticket', 'resource_id')
    def post(self, request):
        if not isinstance(request.json.ticket, str) or ticket_user(request.json.ticket) != request.user.pk:
            raise PickerErrors.INVALID_SESSION
        if not isinstance(request.json.resource_id, str) or not request.json.resource_id:
            raise ResourceErrors.RESOURCE_NOT_FOUND
        resource = picker_resource(request.user, request.json.resource_id)
        from Resource.songs import create_share
        return reply(share_info(create_share(resource)))
