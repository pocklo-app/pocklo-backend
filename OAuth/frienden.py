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
from Resource.models import Resource
from User.models import FriendenBinding, FriendenBindingFlow, FriendenLoginFlow, User


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
    return {'linked': phone is not None, 'phone': f'+86 •••••••{phone[-4:]}' if phone else None,
            'can_unlink': bool(user.qt_user_app_id)}


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


def authorization_url(state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
    params = {'client_id': CLIENT_ID, 'redirect_uri': CALLBACK, 'response_type': 'code',
              'scope': 'phone', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'}
    return AUTHORIZE + '?' + urlencode(params)


class FriendenIdentityError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def verified_identity(code, verifier):
    if not code:
        raise FriendenIdentityError('已取消友间授权。')
    try:
        identity = read_identity(code, verifier)
    except (requests.RequestException, ValueError):
        raise FriendenIdentityError('暂时无法验证友间授权，请重试。', 502) from None
    subject, phone = identity.get('sub'), identity.get('phone')
    if not isinstance(subject, str) or len(subject) > 255 or not SUBJECT.fullmatch(subject):
        raise FriendenIdentityError('无法验证友间账号。')
    if identity.get('phone_verified') is not True or not isinstance(phone, str) or not PHONE.fullmatch(phone):
        raise FriendenIdentityError('请先在友间验证手机号，再重试。')
    return subject, phone


class FriendenBindingView(View):
    @Auth.require_login
    def get(self, request):
        return reply(binding_status(request.user))

    @Auth.require_login
    def post(self, request):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            FriendenBindingFlow.objects.update_or_create(user=request.user, defaults={
                'state_hash': digest(state), 'verifier': verifier, 'claimed': False,
                'expires_at': timezone.now() + timedelta(minutes=10),
            })
        return reply({'url': authorization_url(state, verifier), 'state': state})

    @Auth.require_login
    def delete(self, request):
        with transaction.atomic():
            user = User.objects.select_for_update().get(pk=request.user.pk)
            if not user.qt_user_app_id:
                return reply(message='友间是此账号的唯一登录方式，暂不能解绑。', status=409)
            FriendenBindingFlow.objects.filter(user=user).delete()
            FriendenBinding.objects.filter(user=request.user).delete()
        return reply(binding_status(request.user))


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
            try:
                subject, phone = verified_identity(code, flow.verifier)
            except FriendenIdentityError as error:
                return reply(message=str(error), status=error.status)

            try:
                with transaction.atomic():
                    User.objects.select_for_update().get(pk=request.user.pk)
                    if not flow_query.filter(claimed=True, expires_at__gt=timezone.now()).exists():
                        return reply(message='授权已失效，请重新发起绑定。', status=400)
                    binding = FriendenBinding.objects.filter(user=request.user).first()
                    if (binding and binding.subject != subject and (
                            binding.phone != phone
                            or FriendenBinding.objects.filter(subject=subject).exists())):
                        return reply(message='请先解绑当前友间账号，再绑定其他账号。', status=409)
                    if not binding:
                        FriendenBinding.objects.create(user=request.user, subject=subject, phone=phone)
                    elif binding.phone != phone:
                        binding.phone = phone
                        binding.save(update_fields=['phone'])
            except IntegrityError:
                return reply(message='该手机号或友间账号已绑定其他小兜账号。', status=409)
            return reply(binding_status(request.user))
        finally:
            flow_query.delete()


class FriendenLoginView(View):
    def post(self, request):
        state = secrets.token_urlsafe(32)
        browser_key = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        now = timezone.now()
        FriendenLoginFlow.objects.filter(expires_at__lte=now).delete()
        FriendenLoginFlow.objects.create(
            state_hash=digest(state), browser_hash=digest(browser_key), verifier=verifier,
            expires_at=now + timedelta(minutes=10),
        )
        return reply({'url': authorization_url(state, verifier), 'state': state, 'browser_key': browser_key})


class FriendenLoginCompleteView(View):
    @analyse.json(Validator('state', '授权状态'), Validator('code', '授权码'),
                  Validator('browser_key', '浏览器凭据'))
    def post(self, request):
        state, code, browser_key = request.json.state, request.json.code, request.json.browser_key
        if (not isinstance(state, str) or not STATE.fullmatch(state)
                or not isinstance(browser_key, str) or not STATE.fullmatch(browser_key)
                or not isinstance(code, str) or len(code) > 1024):
            return reply(message='授权回调无效，请重新登录。', status=400)

        flow = FriendenLoginFlow.objects.filter(
            state_hash=digest(state), browser_hash=digest(browser_key), expires_at__gt=timezone.now(),
        )
        verifier = flow.values_list('verifier', flat=True).first()
        if not verifier or not flow.delete()[0]:
            return reply(message='授权已失效，请重新登录。', status=400)

        try:
            subject, phone = verified_identity(code, verifier)
            user = self.account(subject, phone)
        except FriendenIdentityError as error:
            return reply(message=str(error), status=error.status)
        return reply(Auth.get_login_token(user))

    @staticmethod
    def account(subject, phone):
        try:
            with transaction.atomic():
                if FriendenBinding.objects.filter(subject=subject).exclude(phone=phone).exists():
                    raise FriendenIdentityError('友间手机号已变更，请先在原小兜账号的设置中同步手机号。', 409)
                binding = FriendenBinding.objects.select_for_update().filter(phone=phone).first()
                if binding:
                    return binding.user
                user = User.objects.create(nickname='友间用户', qtb_token='')
                FriendenBinding.objects.create(user=user, subject=subject, phone=phone)
                Resource.create_folder('我的文件', user, Resource.get_by_pk(Resource.ROOT_ID))
                return user
        except IntegrityError:
            if FriendenBinding.objects.filter(subject=subject).exclude(phone=phone).exists():
                raise FriendenIdentityError('友间手机号已变更，请先在原小兜账号的设置中同步手机号。', 409) from None
            binding = FriendenBinding.objects.filter(phone=phone).first()
            if binding:
                return binding.user
            raise FriendenIdentityError('暂时无法建立手机号映射，请重新登录。', 409) from None
