"""Link a signed-in Pocklo account to a verified Frienden identity."""
import base64
import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.views import View
from smartdjango import Validator, analyse

from Base.auth import Auth
from User.models import FriendenBinding, FriendenBindingFlow, User


CLIENT_ID = 'pocklo'
CALLBACK = 'https://pocklo.jyonn.space/oauth/frienden/callback'
AUTHORIZE = 'https://frienden.jyonn.space/oauth/authorize'
PROVIDER = 'https://api.sermo.jyonn.space/users/oauth/'
STATE = re.compile(r'[A-Za-z0-9_-]{43}')
SUBJECT = re.compile(r'sermo:[0-9]+:[0-9]+')
PHONE = re.compile(r'\+861[3-9][0-9]{9}')


def reply(body=None, message='', status=200):
    response = JsonResponse({'code': int(bool(message)), 'body': body, 'msg': message}, status=status)
    add_never_cache_headers(response)
    return response


def digest(value):
    return hashlib.sha256(value.encode('ascii')).hexdigest()


def binding_status(user):
    phone = FriendenBinding.objects.filter(user=user).values_list('phone', flat=True).first()
    return {'linked': phone is not None, 'phone': f'+86 •••••••{phone[-4:]}' if phone else None}


def provider(method, endpoint, *, form=None, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    with requests.request(method, PROVIDER + endpoint, data=form, headers=headers,
                          timeout=(4, 8), allow_redirects=False) as response:
        if response.status_code != 200:
            raise requests.HTTPError('Frienden OAuth request failed')
        return response.json()


def read_identity(code, verifier):
    access_token = None
    try:
        token_response = provider('POST', 'token', form={
            'grant_type': 'authorization_code', 'client_id': CLIENT_ID, 'redirect_uri': CALLBACK,
            'code': code, 'code_verifier': verifier,
        })
        if isinstance(token_response, dict):
            access_token = token_response.get('access_token')
        if not isinstance(access_token, str) or not access_token:
            raise ValueError('Missing access token')
        identity = provider('GET', 'userinfo', token=access_token)
        if not isinstance(identity, dict):
            raise ValueError('Invalid identity')
        return identity
    finally:
        if isinstance(access_token, str) and access_token:
            try:
                provider('POST', 'revoke', token=access_token)
            except (requests.RequestException, ValueError):
                pass


class FriendenBindingView(View):
    @Auth.require_login
    def get(self, request):
        return reply(binding_status(request.user))

    @Auth.require_login
    def post(self, request):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            FriendenBindingFlow.objects.update_or_create(user=request.user, defaults={
                'state_hash': digest(state), 'verifier': verifier, 'claimed': False,
                'expires_at': timezone.now() + timedelta(minutes=10),
            })
        params = {'client_id': CLIENT_ID, 'redirect_uri': CALLBACK, 'response_type': 'code',
                  'scope': 'phone', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
        return reply({'url': AUTHORIZE + '?' + urlencode(params), 'state': state})

    @Auth.require_login
    def delete(self, request):
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            FriendenBindingFlow.objects.filter(user=request.user).delete()
            FriendenBinding.objects.filter(user=request.user).delete()
        return reply({'linked': False, 'phone': None})


class FriendenBindingCompleteView(View):
    @analyse.json(Validator('state', '授权状态'), Validator('code', '授权码'))
    @Auth.require_login
    def post(self, request):
        state, code = request.json.state, request.json.code
        if not isinstance(state, str) or not STATE.fullmatch(state) or not isinstance(code, str) or len(code) > 1024:
            return reply(message='授权回调无效，请重新发起绑定。', status=400)

        flow_query = FriendenBindingFlow.objects.filter(user=request.user, state_hash=digest(state))
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            flow = flow_query.filter(claimed=False, expires_at__gt=timezone.now()).first()
            if not flow:
                return reply(message='授权已失效，请重新发起绑定。', status=400)
            flow.claimed = True
            flow.save(update_fields=['claimed'])

        try:
            if not code:
                return reply(message='已取消友间授权。', status=400)
            try:
                identity = read_identity(code, flow.verifier)
            except (requests.RequestException, ValueError):
                return reply(message='暂时无法验证友间授权，请重新发起绑定。', status=502)
            subject, phone = identity.get('sub'), identity.get('phone')
            if not isinstance(subject, str) or len(subject) > 255 or not SUBJECT.fullmatch(subject):
                return reply(message='无法验证友间账号。', status=400)
            if identity.get('phone_verified') is not True or not isinstance(phone, str) or not PHONE.fullmatch(phone):
                return reply(message='请先在友间验证手机号，再重新绑定。', status=400)

            try:
                with transaction.atomic():
                    User.objects.select_for_update().get(pk=request.user.pk)
                    if not flow_query.filter(claimed=True, expires_at__gt=timezone.now()).exists():
                        return reply(message='授权已失效，请重新发起绑定。', status=400)
                    binding = FriendenBinding.objects.filter(user=request.user).first()
                    if binding and binding.subject != subject:
                        return reply(message='请先解绑当前友间账号，再绑定其他账号。', status=409)
                    FriendenBinding.objects.update_or_create(user=request.user, defaults={'subject': subject, 'phone': phone})
            except IntegrityError:
                return reply(message='该友间账号已绑定其他小兜账号。', status=409)
            return reply(binding_status(request.user))
        finally:
            flow_query.delete()
