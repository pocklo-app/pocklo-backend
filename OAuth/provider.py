"""Pocklo as an OAuth provider. Upstream login flows live in separate views."""
import base64
import hashlib
import json
import re
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jwt
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views import View
from smartdjango import Error

from Base.auth import Auth
from Base.common import ADMIN_QITIAN
from OAuth.models import Application, AuthorizationRequest, Credential, Grant, ShareCreation
from Resource.models import Resource, Share
from Resource.validators import RtypeChoice
from Resource.workspace import entry, search, share_info


READ = 'files.read'
SHARE = 'shares.create'
SCOPES = (READ, SHARE)
PERMISSION_VERSION = 1
ACCESS_TTL = 900
REFRESH_TTL = 30 * 86400
CODE_TTL = 120
REQUEST_TTL = 600
DOWNLOAD_TTL = 300


class OAuthError(Exception):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status


def response(body, status=200):
    result = JsonResponse(body, status=status)
    add_never_cache_headers(result)
    result['Referrer-Policy'] = 'no-referrer'
    return result


class ProviderView(View):
    def dispatch(self, request, *args, **kwargs):
        try:
            result = super().dispatch(request, *args, **kwargs)
        except OAuthError as error:
            result = response({'error': error.code}, error.status)
            if error.code == 'invalid_client':
                result['WWW-Authenticate'] = 'Basic realm="Pocklo"'
            elif error.code in ('invalid_token', 'insufficient_scope'):
                result['WWW-Authenticate'] = f'Bearer realm="Pocklo", error="{error.code}"'
        add_never_cache_headers(result)
        result['Referrer-Policy'] = 'no-referrer'
        return result


def account_required(method):
    @wraps(method)
    def authenticated(self, request, *args, **kwargs):
        try:
            Auth._extract_user(request)
        except (Error, jwt.PyJWTError):
            raise OAuthError('login_required', 401)
        return method(self, request, *args, **kwargs)
    return authenticated


def body(request):
    if request.content_type == 'application/x-www-form-urlencoded':
        if any(len(values) != 1 for _, values in request.POST.lists()):
            raise OAuthError('invalid_request')
        return request.POST.dict()
    if request.content_type != 'application/json':
        raise OAuthError('invalid_request')
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeError):
        raise OAuthError('invalid_request')
    if not isinstance(data, dict):
        raise OAuthError('invalid_request')
    return data


def clean_string(value, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise OAuthError('invalid_request')
    return value.strip()


def https_url(value, *, redirect=False):
    value = clean_string(value, 500)
    if any(ord(char) <= 32 or ord(char) > 126 or char in '\\#' for char in value):
        raise OAuthError('invalid_url')
    try:
        URLValidator(schemes=['https'])(value)
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username is not None or url.port == 0:
            raise ValueError()
        if redirect and {'code', 'error', 'state'} & dict(parse_qsl(url.query)).keys():
            raise ValueError()
    except (ValueError, ValidationError):
        raise OAuthError('invalid_url')
    return value


def redirect_list(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 10:
        raise OAuthError('invalid_redirect_uris')
    return list(dict.fromkeys(https_url(value, redirect=True) for value in values))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def limit(bucket, key, maximum, seconds):
    cache_key = f'pocklo-oauth:{bucket}:{digest(key)}'
    if cache.add(cache_key, 1, seconds):
        return
    try:
        count = cache.incr(cache_key)
    except ValueError:
        return
    if count > maximum:
        raise OAuthError('rate_limit_exceeded', 429)


def can_review(user):
    configured = settings.POCKLO_OAUTH_ADMIN_USER_IDS
    if configured:
        return user.pk in configured
    return bool(ADMIN_QITIAN and user.qt_user_app_id == ADMIN_QITIAN)


def application_info(app, *, private=True):
    info = {'name': app.name, 'website': app.website, 'purpose': app.purpose}
    if private:
        info.update(client_id=app.pk, redirect_uris=app.redirect_uris, enabled=app.enabled,
                    review=app.review, review_reason=app.review_reason, revision=app.revision)
    return info


def allow_scope(app, scope):
    if not app.enabled:
        raise OAuthError('application_disabled', 403)
    if SHARE in scope.split() and app.review != 'approved':
        raise OAuthError('application_approval_required', 403)


class Applications(ProviderView):
    @account_required
    def get(self, request):
        return response({
            'applications': [application_info(app) for app in Application.objects.filter(
                developer=request.user).order_by('name', 'pk')],
            'can_review': can_review(request.user),
        })

    @account_required
    def post(self, request):
        limit('register', str(request.user.pk), 20, 3600)
        data = body(request)
        if type(data.get('request_sharing', False)) is not bool:
            raise OAuthError('invalid_request')
        secret = secrets.token_urlsafe(32)
        app = Application.objects.create(
            client_id='pocklo_' + secrets.token_hex(18),
            developer=request.user,
            name=clean_string(data.get('name'), 80),
            website=https_url(data.get('website')),
            purpose=clean_string(data.get('purpose'), 500),
            redirect_uris=redirect_list(data.get('redirect_uris')),
            secret_hash=digest(secret),
            review='pending' if data.get('request_sharing') else 'none',
        )
        return response({'application': application_info(app), 'client_secret': secret}, 201)


class ApplicationSettings(ProviderView):
    @account_required
    @transaction.atomic
    def put(self, request, client_id):
        app = Application.objects.select_for_update().filter(pk=client_id, developer=request.user).first()
        if not app:
            raise OAuthError('application_not_found', 404)
        data = body(request)
        if type(data.get('revision')) is not int or data['revision'] != app.revision or data.get('confirmed') is not True:
            raise OAuthError('application_changed', 409)
        action = data.get('action')
        result = {}
        if action == 'request_sharing' and app.review in ('none', 'rejected', 'revoked'):
            app.review, app.review_reason = 'pending', ''
        elif action == 'configure':
            uris = redirect_list(data.get('redirect_uris'))
            if uris == app.redirect_uris:
                return response({'application': application_info(app)})
            app.redirect_uris = uris
            if app.review == 'approved':
                app.review, app.review_reason = 'pending', ''
        elif action == 'rotate_secret':
            result['client_secret'] = secrets.token_urlsafe(32)
            app.secret_hash = digest(result['client_secret'])
        else:
            raise OAuthError('invalid_action')
        app.revision += 1
        app.save()
        return response({'application': application_info(app), **result})


class AdminApplications(ProviderView):
    @staticmethod
    def require_admin(request):
        if not can_review(request.user):
            raise OAuthError('admin_required', 403)

    @account_required
    def get(self, request):
        self.require_admin(request)
        return response({'applications': [
            {**application_info(app), 'developer_id': app.developer_id}
            for app in Application.objects.order_by('name', 'pk')
        ]})

    @account_required
    @transaction.atomic
    def put(self, request, client_id):
        self.require_admin(request)
        app = Application.objects.select_for_update().filter(pk=client_id).first()
        if not app:
            raise OAuthError('application_not_found', 404)
        data = body(request)
        if type(data.get('revision')) is not int or data['revision'] != app.revision or data.get('confirmed') is not True:
            raise OAuthError('application_changed', 409)
        action = data.get('action')
        reason = clean_string(data.get('reason', ''), 500, required=action == 'reject')
        if action in ('approve', 'reject') and app.review == 'pending':
            app.review = 'approved' if action == 'approve' else 'rejected'
            app.review_reason = reason
        elif action == 'revoke_sharing' and app.review == 'approved':
            app.review, app.review_reason = 'revoked', reason
        elif action == 'disable' and app.enabled:
            app.enabled = False
        elif action == 'enable' and not app.enabled:
            app.enabled = True
        else:
            raise OAuthError('invalid_action')
        app.revision += 1
        app.save()
        return response({'application': {**application_info(app), 'developer_id': app.developer_id}})


class AuthorizationStart(ProviderView):
    def get(self, request):
        if any(len(values) != 1 for _, values in request.GET.lists()):
            raise OAuthError('invalid_request')
        data = request.GET
        if data.get('response_type') != 'code' or data.get('code_challenge_method') != 'S256':
            raise OAuthError('unsupported_authorization_request')
        state = data.get('state', '')
        challenge = data.get('code_challenge', '')
        if not 16 <= len(state) <= 256 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', challenge):
            raise OAuthError('invalid_request')
        app = Application.objects.filter(pk=data.get('client_id')).first()
        if not app or app.pk != data.get('client_id'):
            raise OAuthError('unknown_client')
        limit('authorize', app.pk, 120, 60)
        uri = data.get('redirect_uri')
        if uri not in app.redirect_uris:
            raise OAuthError('redirect_uri_mismatch')
        requested = set(data.get('scope', READ).split())
        if not requested or not requested.issubset(SCOPES):
            raise OAuthError('invalid_scope')
        scope = ' '.join(value for value in SCOPES if value in requested)
        allow_scope(app, scope)
        item = AuthorizationRequest.objects.create(
            application=app, redirect_uri=uri, scope=scope, revision=app.revision,
            state=state, challenge=challenge, expires_at=timezone.now() + timedelta(seconds=REQUEST_TTL),
        )
        return HttpResponseRedirect(f'{settings.POCKLO_OAUTH_WEB_ORIGIN}/oauth/authorize/{item.pk}')


def callback(item, **params):
    uri = urlsplit(item.redirect_uri)
    result = urlencode({**params, 'state': item.state})
    query = f'{uri.query}&{result}' if uri.query else result
    return urlunsplit(uri._replace(query=query))


def issue(grant, kind, ttl, **fields):
    value = secrets.token_urlsafe(32)
    Credential.objects.create(digest=digest(value), grant=grant, kind=kind, generation=grant.generation,
                              expires_at=timezone.now() + timedelta(seconds=ttl), **fields)
    return value


class Consent(ProviderView):
    @staticmethod
    def locked_request(request, uid):
        app_id = AuthorizationRequest.objects.filter(pk=uid).values_list('application_id', flat=True).first()
        app = Application.objects.select_for_update().filter(pk=app_id).first()
        if not app:
            raise OAuthError('authorization_request_expired', 404)
        item = AuthorizationRequest.objects.select_for_update().filter(pk=uid).first()
        if not item or item.expires_at <= timezone.now() or item.revision != app.revision:
            raise OAuthError('authorization_request_expired', 404)
        if item.user_id is not None and item.user_id != request.user.pk:
            raise OAuthError('authorization_request_expired', 404)
        allow_scope(app, item.scope)
        return app, item

    @account_required
    @transaction.atomic
    def get(self, request, uid):
        app, item = self.locked_request(request, uid)
        if not item.user_id:
            item.user = request.user
            item.save(update_fields=['user'])
        return response({
            'application': application_info(app, private=False), 'redirect_uri': item.redirect_uri,
            'scope': item.scope, 'permission_version': PERMISSION_VERSION,
            'account': request.user.nickname or str(request.user.pk),
        })

    @account_required
    @transaction.atomic
    def post(self, request, uid):
        app, item = self.locked_request(request, uid)
        data = body(request)
        if (item.user_id != request.user.pk or type(data.get('permission_version')) is not int
                or data['permission_version'] != PERMISSION_VERSION
                or type(data.get('approve')) is not bool):
            raise OAuthError('invalid_consent')
        if data['approve']:
            grant, created = Grant.objects.get_or_create(
                application=app, user=request.user, defaults={'scope': item.scope, 'revision': app.revision})
            if not created:
                grant.scope, grant.revision, grant.revoked = item.scope, app.revision, False
                grant.generation += 1
                grant.save()
            code = issue(grant, 'code', CODE_TTL, redirect_uri=item.redirect_uri, challenge=item.challenge)
            url = callback(item, code=code)
        else:
            url = callback(item, error='access_denied')
        item.delete()
        return response({'redirect_uri': url})


def basic_app(request):
    authorization = request.headers.get('Authorization', '')
    scheme, _, encoded = authorization.partition(' ')
    if scheme.lower() != 'basic' or len(encoded) > 256:
        raise OAuthError('invalid_client', 401)
    try:
        client_id, secret = base64.b64decode(encoded, validate=True).decode('ascii').split(':', 1)
    except (ValueError, UnicodeError):
        raise OAuthError('invalid_client', 401)
    app = Application.objects.select_for_update().filter(pk=client_id).first()
    if not app or app.pk != client_id or not app.enabled or not secrets.compare_digest(app.secret_hash, digest(secret)):
        raise OAuthError('invalid_client', 401)
    return app


def active(grant, app):
    return app.enabled and not grant.revoked and grant.revision == app.revision


def find_credential(app, value, kinds):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
        return None
    return Credential.objects.select_related('grant').filter(
        digest=digest(value), kind__in=kinds, grant__application=app,
        expires_at__gt=timezone.now(),
    ).first()


def revoke_grant(grant):
    grant.revoked = True
    grant.generation += 1
    grant.save()


class Token(ProviderView):
    @transaction.atomic
    def post(self, request):
        app = basic_app(request)
        data = body(request)
        grant_type = data.get('grant_type')
        if grant_type == 'authorization_code':
            credential = find_credential(app, data.get('code'), ('code',))
        elif grant_type == 'refresh_token':
            credential = find_credential(app, data.get('refresh_token'), ('refresh',))
        else:
            raise OAuthError('unsupported_grant_type')
        if not credential or not active(credential.grant, app) or credential.generation != credential.grant.generation:
            raise OAuthError('invalid_grant')
        grant = credential.grant
        if credential.used:
            if credential.kind == 'refresh':
                revoke_grant(grant)
                return response({'error': 'invalid_grant'}, 400)
            raise OAuthError('invalid_grant')
        if credential.kind == 'code':
            verifier = data.get('code_verifier', '')
            if not isinstance(verifier, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}', verifier):
                raise OAuthError('invalid_grant')
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            if data.get('redirect_uri') != credential.redirect_uri or not secrets.compare_digest(challenge, credential.challenge):
                raise OAuthError('invalid_grant')
        elif 'scope' in data and data['scope'] != grant.scope:
            raise OAuthError('invalid_scope')
        credential.used = True
        credential.save(update_fields=['used'])
        return response({'access_token': issue(grant, 'access', ACCESS_TTL),
                         'refresh_token': issue(grant, 'refresh', REFRESH_TTL),
                         'token_type': 'Bearer', 'expires_in': ACCESS_TTL, 'scope': grant.scope})


class Introspect(ProviderView):
    @transaction.atomic
    def post(self, request):
        app = basic_app(request)
        data = body(request)
        item = find_credential(app, data.get('token'), ('access', 'refresh'))
        if not item or item.used or item.generation != item.grant.generation or not active(item.grant, app):
            return response({'active': False})
        return response({
            'active': True, 'client_id': app.pk, 'scope': item.grant.scope,
            'token_type': 'Bearer' if item.kind == 'access' else 'refresh_token',
            'exp': int(item.expires_at.timestamp()),
        })


class Revoke(ProviderView):
    @transaction.atomic
    def post(self, request):
        app = basic_app(request)
        data = body(request)
        if not isinstance(data.get('token'), str):
            raise OAuthError('invalid_request')
        item = find_credential(app, data['token'], ('access', 'refresh'))
        if item and item.generation == item.grant.generation and active(item.grant, app):
            revoke_grant(item.grant)
        return response({})


class OwnerGrants(ProviderView):
    @account_required
    def get(self, request):
        return response({'grants': [
            {'id': str(grant.pk), 'name': grant.application.name, 'scope': grant.scope,
             'active': active(grant, grant.application), 'updated_at': grant.updated_at.isoformat()}
            for grant in Grant.objects.filter(user=request.user).select_related('application').order_by('-updated_at')
        ]})


class OwnerGrant(ProviderView):
    @account_required
    @transaction.atomic
    def delete(self, request, uid):
        grants = Grant.objects.filter(pk=uid, user=request.user)
        app_id = grants.values_list('application_id', flat=True).first()
        app = Application.objects.select_for_update().filter(pk=app_id).first()
        grant = grants.first() if app else None
        if not grant:
            raise OAuthError('grant_not_found', 404)
        if not grant.revoked:
            revoke_grant(grant)
        return response({})


def bearer_grant(request, scope):
    scheme, _, value = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value):
        raise OAuthError('invalid_token', 401)
    token_hash = digest(value)
    app_id = Credential.objects.filter(pk=token_hash, kind='access').values_list(
        'grant__application_id', flat=True).first()
    app = Application.objects.select_for_update().filter(pk=app_id).first()
    credential = find_credential(app, value, ('access',)) if app else None
    if not credential or not active(credential.grant, app) or credential.generation != credential.grant.generation:
        raise OAuthError('invalid_token', 401)
    grant = credential.grant
    if scope not in grant.scope.split():
        raise OAuthError('insufficient_scope', 403)
    allow_scope(app, scope)
    return grant


def resource_for(grant, resource_id=None):
    owned = Resource.objects.filter(owner_id=grant.user_id).exclude(pk=Resource.ROOT_ID)
    if resource_id is None:
        resource = owned.filter(parent_id=Resource.ROOT_ID, rtype=RtypeChoice.FOLDER).first()
    else:
        resource = owned.filter(res_str_id=resource_id).first()
    if not resource:
        raise OAuthError('resource_not_found', 404)
    return resource


class Files(ProviderView):
    @transaction.atomic
    def get(self, request, resource_id=None):
        grant = bearer_grant(request, READ)
        resource = resource_for(grant, resource_id)
        result = {'resource': entry(resource), 'entries': [], 'next_offset': None, 'has_more': False}
        if resource.rtype == RtypeChoice.FOLDER:
            query = request.GET.get('q', '').strip()
            if query:
                result['entries'], result['has_more'] = search(resource, query[:256])
            else:
                offset = request.GET.get('offset', '0')
                if not re.fullmatch(r'[0-9]{1,8}', offset):
                    raise OAuthError('invalid_offset')
                offset = int(offset)
                rows = list(Resource.objects.filter(parent=resource, owner_id=grant.user_id)
                            .exclude(pk=Resource.ROOT_ID).order_by('rname', 'pk')[offset:offset + 101])
                result['entries'] = [entry(child) for child in rows[:100]]
                result['has_more'] = len(rows) > 100
                if result['has_more']:
                    result['next_offset'] = offset + 100
        return response(result)


class Download(ProviderView):
    @transaction.atomic
    def get(self, request, resource_id):
        resource = resource_for(bearer_grant(request, READ), resource_id)
        if resource.rtype == RtypeChoice.LINK:
            try:
                target = urlsplit(resource.dlpath or '')
                if target.scheme not in ('http', 'https') or not target.hostname:
                    raise ValueError()
            except ValueError:
                raise OAuthError('resource_not_downloadable')
            return response({'kind': 'link', 'url': resource.dlpath})
        if resource.rtype != RtypeChoice.FILE or not resource.dlpath:
            raise OAuthError('resource_not_downloadable')
        return response({'kind': 'download', 'url': resource.get_dl_url(expires=DOWNLOAD_TTL, attachment=True),
                         'expires_in': DOWNLOAD_TTL})


class Shares(ProviderView):
    @transaction.atomic
    def post(self, request):
        grant = bearer_grant(request, SHARE)
        data = body(request)
        resource_id = clean_string(data.get('resource_id'), 64)
        key = request.headers.get('Idempotency-Key', '')
        if not re.fullmatch(r'[!-~]{1,128}', key):
            raise OAuthError('idempotency_key_required')
        key_hash = digest(key)
        previous = ShareCreation.objects.select_related('share__resource').filter(grant=grant, key_hash=key_hash).first()
        if previous:
            if previous.resource_id_at_creation != resource_id:
                raise OAuthError('idempotency_conflict', 409)
            if not previous.share:
                raise OAuthError('share_revoked', 410)
            share = previous.share
        else:
            share = Share.objects.create(resource=resource_for(grant, resource_id))
            ShareCreation.objects.create(grant=grant, key_hash=key_hash, resource_id_at_creation=resource_id, share=share)
        return response({**share_info(share), 'url': f'{settings.POCKLO_OAUTH_WEB_ORIGIN}/share/{share.token}'},
                        200 if previous else 201)
