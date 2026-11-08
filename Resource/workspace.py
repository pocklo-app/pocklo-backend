from itertools import islice
from urllib.parse import urlsplit
from uuid import UUID

import requests
from django.db.models import Case, Q, Value, When
from django.http import FileResponse, JsonResponse
from django.utils.cache import add_never_cache_headers
from django.views import View
from smartdjango import Code, Error, analyse

from Base.auth import Auth
from Base.qn_manager import qn_res_manager
from Resource.models import Resource, Share
from Resource.params import ResourceParams
from Resource.validators import ResourceErrors, RtypeChoice, StypeChoice


@Error.register
class WorkspaceErrors:
    SHARE_NOT_FOUND = Error('分享链接不存在或已撤销', code=Code.NotFound)
    INVALID_LINK = Error('仅支持打开 HTTP 或 HTTPS 链接', code=Code.BadRequest)
    PREVIEW_FAILED = Error('无法读取文件预览，可尝试下载', code=Code.BadRequest)


SEARCH_LIMIT = 100
TEXT_LIMIT = 512 * 1024
MEDIA_TTL = 900


def reply(body):
    response = JsonResponse({'code': 0, 'body': body})
    add_never_cache_headers(response)
    return response


def share_info(share):
    return {
        'token': str(share.token),
        'resource_id': share.resource.res_str_id,
        'name': share.resource.rname,
        'folder': share.resource.rtype == RtypeChoice.FOLDER,
        'created_at': share.created_at.isoformat(),
    }


def get_share(token):
    try:
        return Share.objects.select_related('resource').get(token=token)
    except Share.DoesNotExist:
        raise WorkspaceErrors.SHARE_NOT_FOUND


def path_to(resource, root=None):
    """Return the path inside a share, or the owner's ordinary breadcrumb."""
    path = []
    seen = set()
    owner_id = resource.owner_id
    while resource and resource.pk != Resource.ROOT_ID and resource.pk not in seen:
        if resource.owner_id != owner_id:
            break
        seen.add(resource.pk)
        path.append(resource)
        if root and resource.pk == root.pk:
            return list(reversed(path))
        resource = resource.parent
    return [] if root else list(reversed(path))


class WorkspaceAccess:
    def __init__(self, request, share=None):
        self.user = request.user
        self.share = share

    def owns(self, resource):
        return bool(not self.share and self.user and resource.belong(self.user))

    def path(self, resource):
        return path_to(resource, self.share.resource if self.share else None)

    def allows(self, resource):
        return bool(self.path(resource)) if self.share else self.owns(resource)

    def require(self, resource):
        if not self.allows(resource):
            raise ResourceErrors.NOT_READABLE


def preview_type(resource):
    if resource.rtype == RtypeChoice.LINK:
        return 'link'
    if resource.rtype != RtypeChoice.FILE:
        return None
    mime = resource.mime or ''
    name = resource.rname.lower()
    if resource.sub_type == StypeChoice.IMAGE:
        return 'image'
    if resource.sub_type == StypeChoice.VIDEO:
        return 'video'
    if resource.sub_type == StypeChoice.MUSIC:
        return 'audio'
    if mime == 'application/pdf' or name.endswith('.pdf'):
        return 'pdf'
    if mime == 'text/markdown' or name.endswith(('.md', '.markdown', '.mdown')):
        return 'markdown'
    if mime.startswith('text/') or name.endswith(('.txt', '.log', '.csv')):
        return 'text'
    return None


def entry(resource):
    thumbnail = resource.get_cover_urls()[1]
    result = {
        'id': resource.res_str_id,
        'name': resource.rname,
        'folder': resource.rtype == RtypeChoice.FOLDER,
        'preview': preview_type(resource),
        'size': resource.rsize,
        'created_at': resource.create_time.isoformat(),
        'thumbnail': thumbnail,
    }
    return result


def children(resource):
    return Resource.objects.filter(parent=resource, owner_id=resource.owner_id).order_by('rname', 'pk')


def search(resource, query):
    """Walk only visible directories and stop once a page of matches is found."""
    folders = [(resource.pk, '')]
    seen = {resource.pk}
    matches = []
    name_match = Q(rname__icontains=query)
    while folders:
        next_level = []
        level = iter(folders)
        while batch := dict(islice(level, 128)):
            rows = (
                Resource.objects.filter(parent_id__in=batch, owner_id=resource.owner_id)
                .filter(Q(rtype=RtypeChoice.FOLDER) | name_match)
                .annotate(name_matches=Case(When(name_match, then=Value(True)), default=Value(False)))
                .order_by('parent_id', 'rname', 'pk')
            )
            for child in rows.iterator():
                if child.pk in seen:
                    continue
                relative_path = batch[child.parent_id]
                if child.name_matches:
                    if len(matches) == SEARCH_LIMIT:
                        return matches, True
                    matches.append({**entry(child), 'location': relative_path})
                if child.rtype == RtypeChoice.FOLDER:
                    seen.add(child.pk)
                    next_level.append((child.pk, '/'.join(filter(None, (relative_path, child.rname)))))
        folders = next_level
    return matches, False


def workspace(request, resource, access):
    access.require(resource)
    query = request.GET.get('q', '').strip()
    entries = []
    has_more = False
    if resource.rtype == RtypeChoice.FOLDER:
        if query:
            entries, has_more = search(resource, query[:256])
        else:
            visible = list(children(resource))
            visible.sort(key=lambda child: child.rtype != RtypeChoice.FOLDER)
            entries = [entry(child) for child in visible]
    return reply({
        'resource': entry(resource),
        'entries': entries,
        'breadcrumbs': [{'id': item.res_str_id, 'name': item.rname} for item in access.path(resource)],
        'can_manage': access.owns(resource),
        'can_delete': access.owns(resource) and resource.parent_id != Resource.ROOT_ID,
        'has_more': has_more,
    })


class WorkspaceView(View):
    @Auth.maybe_login
    @analyse.argument(ResourceParams.resource_getter)
    def get(self, request, **kwargs):
        return workspace(request, request.argument.resource, WorkspaceAccess(request))


class ShareCollectionView(View):
    @Auth.require_login
    def get(self, request):
        shares = Share.objects.filter(resource__owner=request.user).select_related('resource')
        return reply({'links': [share_info(share) for share in shares.order_by('-created_at', '-pk')]})

    @Auth.require_login
    @analyse.json(ResourceParams.resource_getter)
    def post(self, request):
        resource = request.json.resource
        if not resource.belong(request.user) or resource.pk == Resource.ROOT_ID:
            raise ResourceErrors.RESOURCE_NOT_BELONG
        return reply(share_info(Share.objects.create(resource=resource)))


class ShareView(View):
    @Auth.maybe_login
    def get(self, request, token):
        share = get_share(token)
        resource_id = request.GET.get('resource')
        resource = Resource.get_by_id(resource_id) if resource_id else share.resource
        return workspace(request, resource, WorkspaceAccess(request, share))

    @Auth.require_login
    def delete(self, request, token):
        share = get_share(token)
        if not share.resource.belong(request.user):
            raise ResourceErrors.RESOURCE_NOT_BELONG
        share.delete()
        return reply(None)


def text_preview(url):
    try:
        with requests.get(url, stream=True, timeout=(3, 5), allow_redirects=False) as response:
            if response.status_code != 200:
                raise WorkspaceErrors.PREVIEW_FAILED
            content = bytearray()
            for chunk in response.iter_content(16 * 1024):
                content.extend(chunk)
                if len(content) > TEXT_LIMIT:
                    break
    except (requests.RequestException, OSError):
        raise WorkspaceErrors.PREVIEW_FAILED
    return {
        'text': content[:TEXT_LIMIT].decode('utf-8-sig', errors='replace'),
        'truncated': len(content) > TEXT_LIMIT,
    }


def require_content_access(request, resource):
    share_token = request.GET.get('share')
    share = None
    if share_token:
        try:
            share = get_share(UUID(share_token))
        except ValueError:
            raise WorkspaceErrors.SHARE_NOT_FOUND
    WorkspaceAccess(request, share).require(resource)


class ContentView(View):
    @Auth.maybe_login
    @analyse.argument(ResourceParams.resource_getter)
    def get(self, request, **kwargs):
        resource = request.argument.resource
        require_content_access(request, resource)

        kind = preview_type(resource)
        if resource.rtype == RtypeChoice.FOLDER or not resource.dlpath:
            raise ResourceErrors.REQUIRE_FILE
        if kind == 'link':
            try:
                target = urlsplit(resource.dlpath)
            except ValueError:
                raise WorkspaceErrors.INVALID_LINK
            if target.scheme not in ('http', 'https') or not target.hostname:
                raise WorkspaceErrors.INVALID_LINK
        if request.GET.get('download') == '1':
            return reply({'kind': 'download', 'url': resource.get_dl_url(expires=MEDIA_TTL, attachment=True)})
        if kind == 'link':
            return reply({'kind': kind, 'url': resource.dlpath})
        if kind == 'pdf':
            return reply({'kind': kind})
        if not kind:
            return reply({'kind': 'unsupported'})

        url = qn_res_manager.get_resource_url(resource.dlpath, expires=MEDIA_TTL)
        if kind in ('markdown', 'text'):
            return reply({'kind': kind, **text_preview(url)})
        return reply({'kind': kind, 'url': url})


class PdfView(View):
    @Auth.maybe_login
    @analyse.argument(ResourceParams.resource_getter)
    def get(self, request, **kwargs):
        resource = request.argument.resource
        require_content_access(request, resource)
        if preview_type(resource) != 'pdf' or not resource.dlpath:
            raise WorkspaceErrors.PREVIEW_FAILED

        headers = {'Accept-Encoding': 'identity'}
        byte_range = request.headers.get('Range')
        if byte_range:
            headers['Range'] = byte_range
        url = qn_res_manager.get_resource_url(resource.dlpath, expires=MEDIA_TTL)
        try:
            upstream = requests.get(url, headers=headers, stream=True, timeout=(3, 12), allow_redirects=False)
        except requests.RequestException:
            raise WorkspaceErrors.PREVIEW_FAILED
        if upstream.status_code not in (200, 206, 416):
            upstream.close()
            raise WorkspaceErrors.PREVIEW_FAILED

        response = FileResponse(upstream.raw, status=upstream.status_code, content_type='application/pdf')
        response['Content-Encoding'] = 'identity'
        for header in ('Accept-Ranges', 'Content-Range', 'Content-Length'):
            if header in upstream.headers:
                response[header] = upstream.headers[header]
        add_never_cache_headers(response)
        return response
