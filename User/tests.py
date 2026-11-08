import base64
import hashlib
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.utils import timezone

from Base.auth import Auth
from Resource.models import Resource
from Resource.validators import RtypeChoice, StypeChoice
from User.models import FriendenBinding, FriendenBindingFlow, User


@patch('Base.jtoken.SECRET_KEY', 'local-tests-only-frienden-signing-key')
@patch('Base.jtoken.JWT_ENCODE_ALGO', 'HS256')
class FriendenBindingTests(TestCase):
    binding = '/api/oauth/frienden/binding'
    complete = '/api/oauth/frienden/complete'
    identity = {'sub': 'sermo:1:123', 'phone': '+8613800138000', 'phone_verified': True}

    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create(qt_user_app_id='owner', qtb_token='')
        cls.other = User.objects.create(qt_user_app_id='other', qtb_token='')

    def headers(self, user):
        return {'HTTP_TOKEN': Auth.get_login_token(user)['token']}

    def start(self, user):
        return self.client.post(self.binding, **self.headers(user)).json()['body']

    def finish(self, user, state, code='synthetic-code'):
        return self.client.post(self.complete, {'state': state, 'code': code},
                                content_type='application/json', **self.headers(user))

    def test_verified_phone_is_saved_masked_and_deleted(self):
        self.assertEqual(self.client.post(self.binding).status_code, 401)
        flow = self.start(self.owner)
        parameters = parse_qs(urlsplit(flow['url']).query)
        saved_flow = FriendenBindingFlow.objects.get(user=self.owner)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(saved_flow.verifier.encode()).digest()).decode().rstrip('=')
        self.assertEqual(parameters['scope'], ['phone'])
        self.assertEqual(parameters['code_challenge'], [challenge])
        self.assertEqual(parameters['redirect_uri'], ['https://pocklo.jyonn.space/oauth/frienden/callback'])
        with patch('OAuth.frienden.provider', side_effect=[
            {'access_token': 'synthetic-token'}, self.identity, {'revoked': True},
        ]) as provider:
            response = self.finish(self.owner, flow['state'])
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(provider.call_args_list[-1].args, ('POST', 'revoke'))
        self.assertEqual(FriendenBinding.objects.get(user=self.owner).phone, self.identity['phone'])
        self.assertNotIn(self.identity['phone'], response.content.decode())
        self.assertEqual(response.json()['body'], {'linked': True, 'phone': '+86 •••••••8000', 'can_unlink': True})
        self.assertEqual(self.client.get(self.binding, **self.headers(self.other)).json()['body']['phone'], None)
        self.assertEqual(self.client.delete(self.binding, **self.headers(self.owner)).status_code, 200)
        self.assertFalse(FriendenBinding.objects.exists())

    def test_state_is_account_bound_expires_and_cannot_be_reused(self):
        flow = self.start(self.owner)
        with patch('OAuth.frienden.read_identity', return_value=self.identity) as exchange:
            self.assertEqual(self.finish(self.other, flow['state']).status_code, 400)
            self.assertEqual(self.finish(self.owner, flow['state'], code='').status_code, 400)
            self.assertEqual(self.finish(self.owner, flow['state']).status_code, 400)
            expired = self.start(self.owner)
            FriendenBindingFlow.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
            self.assertEqual(self.finish(self.owner, expired['state']).status_code, 400)
            exchange.assert_not_called()
            valid = self.start(self.owner)
            self.assertEqual(self.finish(self.owner, valid['state']).status_code, 200)
            self.assertEqual(self.finish(self.owner, valid['state']).status_code, 400)
            exchange.assert_called_once()

    def test_binding_rejects_unverified_numbers_and_subject_conflicts(self):
        with patch('OAuth.frienden.read_identity', return_value={**self.identity, 'phone_verified': False}):
            self.assertEqual(self.finish(self.owner, self.start(self.owner)['state']).status_code, 400)
        self.assertFalse(FriendenBinding.objects.exists())
        with patch('OAuth.frienden.read_identity', return_value=self.identity):
            self.assertEqual(self.finish(self.owner, self.start(self.owner)['state']).status_code, 200)
            self.assertEqual(self.finish(self.other, self.start(self.other)['state']).status_code, 409)
        with patch('OAuth.frienden.read_identity', return_value={**self.identity, 'sub': 'sermo:2:123', 'phone': '+8613800138001'}):
            self.assertEqual(self.finish(self.owner, self.start(self.owner)['state']).status_code, 409)
        self.assertEqual(FriendenBinding.objects.get().subject, self.identity['sub'])

    def test_unlink_invalidates_inflight_exchange(self):
        def unlink_during_exchange(*args):
            self.client.delete(self.binding, **self.headers(self.owner))
            return self.identity

        flow = self.start(self.owner)
        with patch('OAuth.frienden.read_identity', side_effect=unlink_during_exchange):
            self.assertEqual(self.finish(self.owner, flow['state']).status_code, 400)
        self.assertFalse(FriendenBinding.objects.exists())


    login = '/api/oauth/frienden/login'
    login_complete = '/api/oauth/frienden/login/complete'

    def start_login(self):
        return self.client.post(self.login).json()['body']

    def finish_login(self, flow, **overrides):
        body = {'state': flow['state'], 'browser_key': flow['browser_key'], 'code': 'synthetic-code', **overrides}
        return self.client.post(self.login_complete, body, content_type='application/json')

    def test_login_matches_phone_across_frienden_spaces(self):
        FriendenBinding.objects.create(user=self.owner, subject=self.identity['sub'], phone=self.identity['phone'])
        with patch('OAuth.frienden.read_identity', return_value={**self.identity, 'sub': 'sermo:21:456'}):
            response = self.finish_login(self.start_login())
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['body']['user']['user_id'], self.owner.pk)
        self.assertEqual(FriendenBinding.objects.count(), 1)
        self.assertEqual(User.objects.count(), 2)

    def test_same_phone_rebinding_keeps_original_subject_for_change_detection(self):
        FriendenBinding.objects.create(user=self.owner, subject=self.identity['sub'], phone=self.identity['phone'])
        with patch('OAuth.frienden.read_identity', return_value={**self.identity, 'sub': 'sermo:21:456'}):
            self.assertEqual(self.finish(self.owner, self.start(self.owner)['state']).status_code, 200)
        self.assertEqual(FriendenBinding.objects.get(user=self.owner).subject, self.identity['sub'])

        other_phone = '+8613800138001'
        FriendenBinding.objects.create(user=self.other, subject='sermo:20:789', phone=other_phone)
        with patch('OAuth.frienden.read_identity', return_value={**self.identity, 'phone': other_phone}):
            self.assertEqual(self.finish_login(self.start_login()).status_code, 409)
        self.assertEqual(FriendenBinding.objects.count(), 2)

    def test_new_frienden_login_creates_root_and_retains_sole_login_method(self):
        Resource.objects.create(pk=Resource.ROOT_ID, owner=self.owner, parent=None, rname='system', res_str_id='system',
                                rtype=RtypeChoice.FOLDER, sub_type=StypeChoice.FOLDER,
                                mime=Resource.FOLDER_MIME, create_time=timezone.now())
        with patch('OAuth.frienden.read_identity', return_value=self.identity):
            response = self.finish_login(self.start_login())
        self.assertEqual(response.status_code, 200, response.content)
        session = response.json()['body']
        user = User.objects.get(pk=session['user']['user_id'])
        self.assertIsNone(user.qt_user_app_id)
        self.assertTrue(session['user']['root_res'])
        self.assertEqual(Resource.get_root_folder(user).res_str_id, session['user']['root_res'])
        self.assertEqual(self.client.get('/api/user/', HTTP_TOKEN=session['token']).status_code, 200)
        self.assertEqual(self.client.delete(self.binding, HTTP_TOKEN=session['token']).status_code, 409)
        self.assertTrue(FriendenBinding.objects.filter(user=user).exists())

    def test_login_cannot_replay_or_use_another_browsers_secret(self):
        flow = self.start_login()
        second = self.start_login()
        self.assertNotIn(flow['browser_key'], flow['url'])
        with patch('OAuth.frienden.read_identity', return_value={**self.identity, 'phone_verified': False}) as exchange:
            self.assertEqual(self.finish_login(flow, browser_key=second['browser_key']).status_code, 400)
            exchange.assert_not_called()
            self.assertEqual(self.finish_login(flow).status_code, 400)
            self.assertEqual(self.finish_login(flow).status_code, 400)
            exchange.assert_called_once()
        self.assertFalse(FriendenBinding.objects.exists())
