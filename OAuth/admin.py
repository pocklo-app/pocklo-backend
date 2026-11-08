"""Separate email and MFA authentication for OAuth platform administrators."""
import base64
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

import pyotp
import requests
from cryptography.fernet import Fernet
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac

from Base import common
from Config.models import CI, Config
from OAuth.models import AdminAccount, Application
from OAuth.provider import (OAuthError, ProviderView, application_info, body,
                            clean_string, digest, limit, response)


CODE_LIFETIME = timedelta(minutes=10)
SESSION_LIFETIME = timedelta(hours=1)
RECENT_LIFETIME = timedelta(minutes=5)


def admin_email(data):
    return clean_string(data.get('email'), 254).lower()


def secret_mac(namespace, value):
    if not common.SECRET_KEY:
        raise OAuthError('admin_auth_unavailable', 503)
    return salted_hmac('pocklo-oauth-admin:' + namespace, value,
                       secret=common.SECRET_KEY, algorithm='sha256')


def otp_hash(email, code):
    return secret_mac('otp', email + '\0' + code).hexdigest()


def vault():
    return Fernet(base64.urlsafe_b64encode(secret_mac('totp', 'encryption').digest()))


def send_code(email, code):
    account = Config.get_value_by_key(CI.NOTIFICATOR_ACCOUNT)
    token = Config.get_value_by_key(CI.NOTIFICATOR_TOKEN)
    host = Config.get_value_by_key(CI.NOTIFICATOR_HOST, '').rstrip('/')
    if not account or not token or urlsplit(host).scheme != 'https':
        raise OAuthError('admin_mail_unavailable', 503)
    try:
        result = requests.post(f'{host}/api/channel/send', headers={'Auth': f'{account}${token}'},
                               json={
                                   'message': {'format': 'verification', 'body': {'code': code, 'time': 10},
                                               'title': '小兜 · 管理员登录验证码', 'locale': 'zh-CN'},
                                   'deliveries': [{'channel': 'mail', 'target': email, 'options': {'locale': 'zh-CN'}}],
                               }, timeout=(3, 12), allow_redirects=False)
        if not result.ok or result.json().get('identifier') != 'OK':
            raise ValueError()
    except (requests.RequestException, ValueError, AttributeError):
        raise OAuthError('admin_mail_unavailable', 503) from None


def current_admin(request, *, recent=False, lock=False):
    scheme, _, token = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise OAuthError('admin_login_required', 401)
    accounts = AdminAccount.objects
    if lock:
        accounts = accounts.select_for_update()
    account = accounts.filter(session_hash=digest(token), email__in=settings.POCKLO_OAUTH_ADMIN_EMAILS,
                              verified_at__gt=timezone.now() - SESSION_LIFETIME).first()
    if not account:
        raise OAuthError('admin_login_required', 401)
    if recent and account.verified_at <= timezone.now() - RECENT_LIFETIME:
        raise OAuthError('admin_reauthentication_required', 403)
    return account


def session_info(account):
    return {'email': account.email, 'mfa_enabled': bool(account.totp_ciphertext)}


def new_session(account):
    token = secrets.token_urlsafe(32)
    account.session_hash = digest(token)
    account.verified_at = timezone.now()
    account.save()
    return {'admin': session_info(account), 'token': token}


def totp_counter(ciphertext, code, after=-1):
    if not valid_digits(code):
        return None
    otp = pyotp.TOTP(vault().decrypt(ciphertext.encode()).decode())
    counter = int(timezone.now().timestamp()) // otp.interval
    for candidate in (counter, counter - 1, counter + 1):
        if candidate > after and secrets.compare_digest(otp.generate_otp(candidate), code):
            return candidate
    return None


def valid_digits(code):
    return isinstance(code, str) and re.fullmatch(r'[0-9]{6}', code) is not None


class EmailCode(ProviderView):
    @transaction.atomic
    def post(self, request):
        email = admin_email(body(request))
        limit('admin-email', request.META.get('REMOTE_ADDR', ''), 20, 3600)
        accepted = {'retry_after': 60, 'expires_in': int(CODE_LIFETIME.total_seconds())}
        if email not in settings.POCKLO_OAUTH_ADMIN_EMAILS:
            return response(accepted)
        account, _ = AdminAccount.objects.get_or_create(email=email)
        account = AdminAccount.objects.select_for_update().get(pk=account.pk)
        if account.totp_ciphertext:
            return response(accepted)
        now = timezone.now()
        if account.code_sent_at and now - account.code_sent_at < timedelta(minutes=1):
            raise OAuthError('rate_limit_exceeded', 429)
        if not account.code_window or now - account.code_window >= timedelta(hours=1):
            account.code_window, account.code_sends = now, 0
        if account.code_sends >= 5:
            raise OAuthError('rate_limit_exceeded', 429)
        code = f'{secrets.randbelow(1_000_000):06d}'
        account.code_hash, account.code_sent_at = otp_hash(email, code), now
        account.code_sends += 1
        account.save()
        send_code(email, code)
        return response(accepted)


class Session(ProviderView):
    def get(self, request):
        return response({'admin': session_info(current_admin(request))})

    @transaction.atomic
    def post(self, request):
        data = body(request)
        email = admin_email(data)
        limit('admin-login', request.META.get('REMOTE_ADDR', ''), 30, 60)
        invalid = {'error': 'invalid_admin_login'}
        if email not in settings.POCKLO_OAUTH_ADMIN_EMAILS:
            return response(invalid, 401)
        account = AdminAccount.objects.select_for_update().filter(pk=email).first()
        if not account:
            return response(invalid, 401)
        now = timezone.now()
        if not account.login_window or now - account.login_window >= timedelta(minutes=5):
            account.login_window, account.login_attempts = now, 0
        if account.login_attempts >= 8:
            raise OAuthError('rate_limit_exceeded', 429)
        method, code = data.get('method'), data.get('code')
        verified = False
        if method == 'email' and not account.totp_ciphertext and valid_digits(code):
            verified = bool(account.code_hash and account.code_sent_at
                            and now - account.code_sent_at < CODE_LIFETIME
                            and secrets.compare_digest(account.code_hash, otp_hash(email, code)))
            if verified:
                account.code_hash = ''
        elif method == 'totp' and account.totp_ciphertext:
            counter = totp_counter(account.totp_ciphertext, code, account.totp_last_counter)
            verified = counter is not None
            if verified:
                account.totp_last_counter = counter
        elif method == 'recovery' and account.totp_ciphertext and isinstance(code, str):
            hashed = digest(code.strip().upper())
            verified = any(secrets.compare_digest(item, hashed) for item in account.recovery_hashes)
            if verified:
                account.recovery_hashes = [item for item in account.recovery_hashes if item != hashed]
        if not verified:
            account.login_attempts += 1
            account.save(update_fields=['login_window', 'login_attempts'])
            return response(invalid, 401)
        account.login_attempts = 0
        account.pending_ciphertext, account.pending_at = '', None
        return response(new_session(account))

    @transaction.atomic
    def delete(self, request):
        account = current_admin(request, lock=True)
        account.session_hash, account.verified_at = '', None
        account.pending_ciphertext, account.pending_at = '', None
        account.save()
        return response({})


class Mfa(ProviderView):
    @transaction.atomic
    def post(self, request):
        account = current_admin(request, recent=True, lock=True)
        data = body(request)
        action = data.get('action')
        if account.totp_ciphertext:
            raise OAuthError('mfa_already_enabled', 409)
        if action == 'setup':
            key = pyotp.random_base32()
            account.pending_ciphertext = vault().encrypt(key.encode()).decode()
            account.pending_at = timezone.now()
            account.save(update_fields=['pending_ciphertext', 'pending_at'])
            return response({'secret': key, 'uri': pyotp.TOTP(key).provisioning_uri(
                name=account.email, issuer_name='Pocklo Admin')})
        if action != 'confirm' or not account.pending_ciphertext or not account.pending_at or (
                timezone.now() - account.pending_at >= RECENT_LIFETIME):
            raise OAuthError('mfa_setup_expired')
        limit('admin-mfa', account.email, 8, 300)
        counter = totp_counter(account.pending_ciphertext, data.get('code'))
        if counter is None:
            raise OAuthError('invalid_mfa_code')
        account.totp_ciphertext = account.pending_ciphertext
        account.pending_ciphertext, account.pending_at = '', None
        account.totp_last_counter = counter
        recovery = [secrets.token_hex(8).upper() for _ in range(8)]
        account.recovery_hashes = [digest(item) for item in recovery]
        return response({**new_session(account), 'recovery_codes': recovery})


class AdminApplications(ProviderView):
    def get(self, request):
        current_admin(request)
        return response({'applications': [
            {**application_info(app), 'developer_id': app.developer_id}
            for app in Application.objects.order_by('name', 'pk')
        ]})

    @transaction.atomic
    def put(self, request, client_id):
        current_admin(request, recent=True, lock=True)
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
