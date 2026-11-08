import base64
import hashlib
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pyotp
from django.test import TestCase, override_settings
from django.utils import timezone

from Base.auth import Auth
from Resource.models import Resource, Share
from Resource.validators import RtypeChoice, StypeChoice
from User.models import FriendenBinding, User

from .models import AdminAccount, Application
from .provider import digest


BASE = '/api/oauth/'
CALLBACK = 'https://app.example/oauth?from=pocklo'
STATE = 'test-state-at-least-16'
VERIFIER = 'v' * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip('=')


@patch('Base.jtoken.SECRET_KEY', 'isolated-pocklo-local-test-signing-key')
@patch('Base.jtoken.JWT_ENCODE_ALGO', 'HS256')
class ProviderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create(nickname='Owner', qtb_token='')
        cls.other = User.objects.create(nickname='Other', qtb_token='')
        cls.root = Resource.objects.create(
            pk=Resource.ROOT_ID, parent=None, owner=cls.owner, rname='root', res_str_id='root00',
            rtype=RtypeChoice.FOLDER, sub_type=StypeChoice.FOLDER, create_time=timezone.now(),
        )
        cls.folder = cls.make_resource('folder', cls.owner, cls.root, folder=True)
        cls.file = cls.make_resource('file00', cls.owner, cls.folder)
        cls.private = cls.make_resource('secret', cls.other, cls.root)

    @classmethod
    def make_resource(cls, identifier, owner, parent, *, folder=False):
        return Resource.objects.create(
            owner=owner, parent=parent, rname=identifier, res_str_id=identifier,
            rtype=RtypeChoice.FOLDER if folder else RtypeChoice.LINK,
            sub_type=StypeChoice.FOLDER if folder else StypeChoice.LINK,
            dlpath=None if folder else 'https://example.org/example',
            create_time=timezone.now(),
        )

    @staticmethod
    def login(user):
        return {'HTTP_TOKEN': Auth.get_login_token(user)['token']}

    def register(self, request_sharing=False, **overrides):
        data = {'name': 'Arbitrary application', 'website': 'https://app.example',
                'purpose': 'Read selected files', 'redirect_uris': [CALLBACK],
                'request_sharing': request_sharing, **overrides}
        return self.client.post(BASE + 'applications/', data, content_type='application/json', **self.login(self.other))

    def begin(self, app, scope='files.read', **overrides):
        return self.client.get(BASE + 'authorize/', {
            'client_id': app['application']['client_id'], 'redirect_uri': CALLBACK,
            'response_type': 'code', 'state': STATE, 'code_challenge_method': 'S256',
            'code_challenge': CHALLENGE, 'scope': scope, **overrides,
        })

    def consent(self, app, scope='files.read', approve=True):
        begin = self.begin(app, scope)
        self.assertEqual(begin.status_code, 302, begin.content)
        request_id = parse_qs(urlsplit(begin.url).query)['request'][0]
        path = BASE + 'requests/' + request_id + '/'
        preview = self.client.get(path, **self.login(self.owner))
        self.assertEqual(preview.status_code, 200, preview.content)
        payload = {'approve': approve, 'permission_version': preview.json()['permission_version']}
        self.assertEqual(self.client.post(path, payload, content_type='application/json',
                                         **self.login(self.other)).status_code, 404)
        decision = self.client.post(path, payload, content_type='application/json', **self.login(self.owner))
        self.assertEqual(decision.status_code, 200, decision.content)
        self.assertEqual(self.client.post(path, payload, content_type='application/json',
                                         **self.login(self.owner)).status_code, 404)
        query = parse_qs(urlsplit(decision.json()['redirect_uri']).query)
        self.assertEqual(query['from'], ['pocklo'])
        self.assertEqual(query['state'], [STATE])
        return query

    def client_auth(self, app):
        pair = app['application']['client_id'] + ':' + app['client_secret']
        return {'HTTP_AUTHORIZATION': 'Basic ' + base64.b64encode(pair.encode()).decode()}

    def exchange(self, app, code, **overrides):
        return self.client.post(BASE + 'token/', urlencode({
            'grant_type': 'authorization_code', 'code': code,
            'redirect_uri': CALLBACK, 'code_verifier': VERIFIER, **overrides,
        }), content_type='application/x-www-form-urlencoded', **self.client_auth(app))

    def refresh(self, app, token):
        return self.client.post(BASE + 'token/', {'grant_type': 'refresh_token', 'refresh_token': token},
                                content_type='application/json', **self.client_auth(app))

    @staticmethod
    def bearer(tokens):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + tokens['access_token']}

    def test_registration_pkce_denial_and_owner_isolation(self):
        created = self.register()
        self.assertEqual(created.status_code, 201, created.content)
        app = created.json()
        self.assertEqual(Application.objects.get().secret_hash, digest(app['client_secret']))
        self.assertNotIn(app['client_secret'], self.client.get(BASE + 'applications/', **self.login(self.other)).content.decode())
        self.assertEqual(self.register(redirect_uris=['https://*.example/oauth']).status_code, 400)
        self.assertEqual(self.begin(app, redirect_uri=CALLBACK + '&extra=1').status_code, 400)
        self.assertEqual(self.begin(app, scope='unknown').status_code, 400)
        self.assertEqual(self.consent(app, approve=False)['error'], ['access_denied'])
        code = self.consent(app)['code'][0]
        self.assertEqual(self.exchange(app, code, code_verifier='z' * 43).status_code, 400)
        exchange = self.exchange(app, code)
        self.assertEqual(exchange.status_code, 200, exchange.content)
        self.assertEqual(self.exchange(app, code).status_code, 400)
        tokens = exchange.json()
        auth = self.bearer(tokens)
        self.assertEqual(self.client.get(BASE + 'files/', **auth).json()['entries'][0]['id'], self.file.res_str_id)
        self.assertEqual(self.client.get(BASE + 'files/secret/', **auth).status_code, 404)
        self.assertEqual(self.client.get(BASE + 'files/file00/download/', **auth).json()['kind'], 'link')
        self.assertEqual(self.client.post(BASE + 'shares/', {}, content_type='application/json', **auth).status_code, 403)
        grant_id = self.client.get(BASE + 'grants/', **self.login(self.owner)).json()['grants'][0]['id']
        self.assertEqual(self.client.delete(BASE + f'grants/{grant_id}/', **self.login(self.other)).status_code, 404)
        self.assertEqual(self.client.delete(BASE + f'grants/{grant_id}/', **self.login(self.owner)).status_code, 200)
        self.assertEqual(self.client.get(BASE + 'files/', **auth).status_code, 401)

    def test_share_approval_and_retry(self):
        app = self.register(request_sharing=True).json()
        client_id = app['application']['client_id']
        scope = 'shares.create'
        self.assertEqual(self.begin(app, scope).status_code, 403)
        url = BASE + f'admin/applications/{client_id}/'
        data = {'action': 'approve', 'confirmed': True, 'revision': 1}
        self.assertEqual(self.client.put(url, data, content_type='application/json', **self.login(self.owner)).status_code, 401)
        token = 'x' * 43
        AdminAccount.objects.create(email='reviewer@example.com', session_hash=digest(token), verified_at=timezone.now())
        with override_settings(POCKLO_OAUTH_ADMIN_EMAILS={'reviewer@example.com'}):
            self.assertEqual(self.client.put(url, data, content_type='application/json',
                                             **self.bearer({'access_token': token})).status_code, 200)
        tokens = self.exchange(app, self.consent(app, scope)['code'][0]).json()
        auth = {**self.bearer(tokens), 'HTTP_IDEMPOTENCY_KEY': 'one-share'}
        payload = {'resource_id': self.file.res_str_id}
        self.assertEqual(self.client.get(BASE + 'files/', **auth).status_code, 403)
        created = self.client.post(BASE + 'shares/', payload, content_type='application/json', **auth)
        self.assertEqual(created.status_code, 201, created.content)
        retry = self.client.post(BASE + 'shares/', payload, content_type='application/json', **auth)
        self.assertEqual(retry.status_code, 200, retry.content)
        self.assertEqual(created.json()['token'], retry.json()['token'])
        self.assertEqual(self.client.post(BASE + 'shares/', {'resource_id': self.folder.res_str_id},
                                         content_type='application/json', **auth).status_code, 409)
        Share.objects.all().delete()
        self.assertEqual(self.client.post(BASE + 'shares/', payload, content_type='application/json', **auth).status_code, 410)

    def test_refresh_replay_cannot_revoke_a_later_authorization(self):
        app = self.register().json()
        old = self.exchange(app, self.consent(app)['code'][0]).json()
        current = self.exchange(app, self.consent(app)['code'][0]).json()
        self.assertEqual(self.refresh(app, old['refresh_token']).status_code, 400)
        refreshed = self.refresh(app, current['refresh_token'])
        self.assertEqual(refreshed.status_code, 200, refreshed.content)
        tokens = refreshed.json()
        status = self.client.post(BASE + 'introspect/', {'token': tokens['access_token']},
                                  content_type='application/json', **self.client_auth(app))
        self.assertTrue(status.json()['active'])
        self.assertEqual(self.refresh(app, current['refresh_token']).status_code, 400)
        self.assertEqual(self.client.get(BASE + 'files/', **self.bearer(tokens)).status_code, 401)

    def test_frienden_account_proof_checks_client_token_and_current_phone(self):
        app = self.register().json()
        other_app = self.register().json()
        phone = '+8613800138000'
        binding = FriendenBinding.objects.create(user=self.owner, subject='sermo:1:1', phone=phone)
        tokens = self.exchange(app, self.consent(app)['code'][0]).json()
        other_tokens = self.exchange(other_app, self.consent(other_app)['code'][0]).json()

        def prove(token, *, client=app, candidate=phone):
            return self.client.post(BASE + 'frienden-account/', {'token': token, 'phone': candidate},
                                    content_type='application/json', **self.client_auth(client))

        self.assertEqual(prove(tokens['access_token']).status_code, 401)
        with override_settings(POCKLO_OAUTH_FRIENDEN_CLIENT_ID=app['application']['client_id']):
            first = prove(tokens['access_token'])
            self.assertEqual(first.status_code, 200, first.content)
            self.assertEqual(set(first.json()), {'sub'})
            self.assertNotIn(phone, first.content.decode())
            self.assertIn('no-store', first['Cache-Control'])
            self.assertEqual(prove(tokens['refresh_token']).status_code, 401)
            self.assertEqual(prove(other_tokens['access_token']).status_code, 401)
            self.assertEqual(prove(other_tokens['access_token'], client=other_app).status_code, 401)
            self.assertEqual(prove(tokens['access_token'], candidate='13800138000').status_code, 400)
            mismatch = prove(tokens['access_token'], candidate='+8613800138001')
            self.assertEqual((mismatch.status_code, mismatch.json()), (409, {'error': 'account_mismatch'}))
            next_tokens = self.exchange(app, self.consent(app)['code'][0]).json()
            self.assertEqual(prove(tokens['access_token']).status_code, 401)
            self.assertEqual(prove(next_tokens['access_token']).json(), first.json())
            binding.delete()
            self.assertEqual(prove(next_tokens['access_token']).status_code, 409)
            self.client.post(BASE + 'revoke/', {'token': next_tokens['access_token']},
                             content_type='application/json', **self.client_auth(app))
            self.assertEqual(prove(next_tokens['access_token']).status_code, 401)


@override_settings(POCKLO_OAUTH_ADMIN_EMAILS={'reviewer@example.com'})
@patch('Base.common.SECRET_KEY', 'isolated-pocklo-admin-test-signing-key')
class AdminLoginTests(TestCase):
    email = 'reviewer@example.com'

    def post(self, route, data, token=None):
        headers = {'HTTP_AUTHORIZATION': 'Bearer ' + token} if token else {}
        return self.client.post(BASE + 'admin/' + route, data, content_type='application/json', **headers)

    @patch('OAuth.admin.send_code')
    def test_email_then_mfa_and_single_use_recovery(self, mail):
        self.assertEqual(self.post('email-code/', {'email': 'outside@example.com'}).status_code, 200)
        mail.assert_not_called()
        self.assertEqual(self.post('email-code/', {'email': self.email}).status_code, 200)
        code = mail.call_args.args[1]
        email_login = {'email': self.email, 'method': 'email', 'code': code}
        response = self.post('session/', email_login)
        self.assertEqual(response.status_code, 200, response.content)
        session = response.json()['token']
        self.assertEqual(self.post('session/', email_login).status_code, 401)

        setup = self.post('mfa/', {'action': 'setup'}, session).json()
        totp = pyotp.TOTP(setup['secret'])
        confirm = self.post('mfa/', {'action': 'confirm', 'code': totp.now()}, session)
        self.assertEqual(confirm.status_code, 200, confirm.content)
        recovery = confirm.json()['recovery_codes'][0]
        self.assertNotIn(recovery, AdminAccount.objects.get(pk=self.email).recovery_hashes)
        self.assertEqual(self.client.get(BASE + 'admin/session/',
                                         HTTP_AUTHORIZATION='Bearer ' + session).status_code, 401)
        self.assertEqual(self.post('session/', {'email': self.email, 'method': 'totp', 'code': totp.now()}).status_code, 401)
        self.assertEqual(self.post('session/', email_login).status_code, 401)
        recovery_login = {'email': self.email, 'method': 'recovery', 'code': recovery}
        self.assertEqual(self.post('session/', recovery_login).status_code, 200)
        self.assertEqual(self.post('session/', recovery_login).status_code, 401)
