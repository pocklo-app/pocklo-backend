from io import BytesIO
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from Base.auth import Auth
from Resource.models import Resource, Share
from Resource.validators import RtypeChoice, StypeChoice
from User.models import User


class WorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create(qt_user_app_id='owner', qtb_token='')
        cls.visitor = User.objects.create(qt_user_app_id='visitor', qtb_token='')
        root = cls.make_resource('system', '000001', None, folder=True)
        cls.directory = cls.make_resource('项目交付', 'delivr', root, folder=True)
        cls.subdirectory = cls.make_resource('资料', 'subdir', cls.directory, folder=True)
        cls.readme = cls.make_resource('使用说明.md', 'readme', cls.subdirectory)
        cls.outside = cls.make_resource('使用说明-内部.md', 'secret', root)
        cls.share = Share.objects.create(resource=cls.directory)

    @classmethod
    def make_resource(cls, name, slug, parent, folder=False, **kwargs):
        return Resource.objects.create(
            owner=cls.owner, parent=parent, rname=name, res_str_id=slug,
            rtype=RtypeChoice.FOLDER if folder else RtypeChoice.FILE,
            sub_type=kwargs.pop('sub_type', StypeChoice.FOLDER if folder else StypeChoice.FILE),
            mime=kwargs.pop('mime', 'application/octet-stream'),
            dlpath=None if folder else 'disk/test/' + name,
            create_time=timezone.now(), **kwargs,
        )

    def share_url(self, share=None):
        return '/api/shares/' + str((share or self.share).token)

    def login_headers(self, user):
        return {'HTTP_TOKEN': Auth.get_login_token(user)['token']}

    @patch('Base.jtoken.SECRET_KEY', 'local-tests-only-test-signing-key')
    @patch('Base.jtoken.JWT_ENCODE_ALGO', 'HS256')
    def test_owner_manages_independent_links(self):
        owner = self.login_headers(self.owner)
        visitor = self.login_headers(self.visitor)
        response = self.client.post('/api/shares/', {'res_str_id': self.directory.res_str_id},
                                    content_type='application/json', **owner)
        self.assertEqual(response.status_code, 200, response.content)
        token = response.json()['body']['token']
        self.assertNotEqual(token, self.directory.res_str_id)
        self.assertNotEqual(token, str(self.share.token))
        self.assertEqual(len(self.client.get('/api/shares/', **owner).json()['body']['links']), 2)
        self.assertEqual(self.client.get('/api/shares/', **visitor).json()['body']['links'], [])
        self.assertEqual(self.client.delete(self.share_url(), **visitor).status_code, 403)
        self.assertEqual(self.client.delete(self.share_url(), **owner).status_code, 200)
        self.assertEqual(self.client.get(self.share_url()).status_code, 404)
        self.assertEqual(self.client.get('/api/shares/' + token).status_code, 200)

    def test_search_reaches_nested_resources_only_in_share(self):
        response = self.client.get(self.share_url(), {'q': '使用说明'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(
            [(item['id'], item['location']) for item in response.json()['body']['entries']],
            [(self.readme.res_str_id, self.subdirectory.rname)],
        )
        self.assertEqual(self.client.get(self.share_url(), {'resource': self.outside.res_str_id}).status_code, 403)
        self.assertEqual(self.client.get('/api/res/readme/workspace').status_code, 403)
        response = self.client.get(self.share_url(), {'resource': self.readme.res_str_id})
        self.assertEqual([item['id'] for item in response.json()['body']['breadcrumbs']],
                         ['delivr', 'subdir', 'readme'])

    @patch('Base.jtoken.SECRET_KEY', 'local-tests-only-test-signing-key')
    @patch('Base.jtoken.JWT_ENCODE_ALGO', 'HS256')
    def test_rename_keeps_link_and_storage_key_while_move_changes_directory_scope(self):
        owner = self.login_headers(self.owner)
        file_share = Share.objects.create(resource=self.readme)
        storage_key = self.readme.dlpath
        response = self.client.put('/api/res/readme', {'rname': '路线.md', 'status': None, 'description': None, 'visit_key': None, 'right_bubble': None}, content_type='application/json', **owner)
        self.assertEqual(response.status_code, 200, response.content)
        self.readme.refresh_from_db()
        self.assertEqual(self.readme.dlpath, storage_key)
        self.assertEqual(self.client.get(self.share_url(file_share)).json()['body']['resource']['name'], '路线.md')
        response = self.client.put('/api/res/readme', {'parent_str_id': '000001'},
                                   content_type='application/json', **owner)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.client.get(self.share_url(), {'resource': 'readme'}).status_code, 403)
        self.assertEqual(self.client.get(self.share_url(file_share)).status_code, 200)

    @patch('Base.jtoken.SECRET_KEY', 'local-tests-only-test-signing-key')
    @patch('Base.jtoken.JWT_ENCODE_ALGO', 'HS256')
    @patch('Base.qn_manager.qn_res_manager.delete_res')
    def test_scoped_delete_refuses_moved_resource(self, delete_object):
        owner = self.login_headers(self.owner)
        target = '/api/res/readme?within=subdir'

        self.assertEqual(self.client.delete('/api/res/subdir?within=subdir', **owner).status_code, 403)
        response = self.client.put('/api/res/readme', {'parent_str_id': 'delivr'},
                                   content_type='application/json', **owner)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.delete(target, **owner).status_code, 403)
        self.assertTrue(Resource.objects.filter(pk=self.readme.pk).exists())
        delete_object.assert_not_called()

        self.assertEqual(self.client.delete('/api/res/readme?within=delivr', **owner).status_code, 200)
        self.assertEqual(self.client.delete('/api/res/subdir?within=subdir', **owner).status_code, 200)
        delete_object.assert_called_once_with(self.readme.dlpath)

    @patch('Resource.workspace.text_preview', return_value={'text': '# 使用说明', 'truncated': False})
    @patch('Resource.workspace.qn_res_manager.get_resource_url', return_value='https://storage.example/object')
    def test_preview_uses_same_scope_as_listing_and_download(self, sign, read):
        params = {'share': str(self.share.token)}
        response = self.client.get('/api/res/readme/content', params)
        self.assertEqual(response.json()['body']['kind'], 'markdown')
        self.assertEqual(response.json()['body']['text'], '# 使用说明')
        self.assertEqual(self.client.get('/api/res/secret/content', params).status_code, 403)
        self.assertEqual(self.client.get('/api/res/secret/content', {**params, 'download': '1'}).status_code, 403)
        self.assertEqual(self.client.get('/api/res/readme/content', {**params, 'download': '1'}).status_code, 200)
        self.assertEqual(read.call_count, 1)

    @patch('Base.qn_manager.qn_res_manager.get_resource_url', return_value='https://storage.example/thumb')
    def test_covers_come_from_media_only(self, sign):
        image = self.make_resource('图.png', 'image1', self.directory, sub_type=StypeChoice.IMAGE)
        video = self.make_resource('视频.mp4', 'video1', self.directory, sub_type=StypeChoice.VIDEO)
        self.assertEqual(self.directory.get_cover_urls(), (None, None))
        self.assertEqual(self.readme.get_cover_urls(), (None, None))
        self.assertIsNotNone(image.get_cover_urls()[1])
        self.assertEqual(sign.call_args.args, (image.dlpath,))
        self.assertTrue(sign.call_args.kwargs['operation'].startswith('imageView2/'))
        self.assertIsNotNone(video.get_cover_urls()[1])
        self.assertEqual(sign.call_args.args, (video.dlpath,))
        self.assertTrue(sign.call_args.kwargs['operation'].startswith('vframe/'))
        sign.reset_mock()
        response = self.client.get('/api/res/image1/base')
        self.assertEqual(response.status_code, 401)
        sign.assert_not_called()

    def test_old_resource_links_have_no_public_access(self):
        for path in ('/api/res/readme', '/api/res/readme/dl', '/api/res/readme/workspace'):
            self.assertIn(self.client.get(path, {'visit_key': 'old-code'}).status_code, (401, 403))
        self.assertEqual(self.client.get('/s/readme').status_code, 404)

    @patch('Resource.workspace.qn_res_manager.get_resource_url', return_value='https://storage.example/object')
    @patch('Resource.workspace.requests.get')
    def test_pdf_supports_ranges_only_inside_the_share(self, fetch, sign):
        pdf = self.make_resource('路线.PDF', 'map001', self.subdirectory)
        fetch.return_value = Mock(
            status_code=206, raw=BytesIO(b'%PDF'), headers={'Content-Range': 'bytes 0-3/120'},
        )
        path = '/api/res/' + pdf.res_str_id
        self.assertEqual(self.client.get(path + '/pdf').status_code, 403)
        fetch.assert_not_called()
        params = {'share': str(self.share.token)}
        self.assertEqual(self.client.get(path + '/content', params).json()['body']['kind'], 'pdf')
        response = self.client.get(path + '/pdf', params, HTTP_RANGE='bytes=0-3')
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['Content-Range'], 'bytes 0-3/120')
        self.assertEqual(b''.join(response.streaming_content), b'%PDF')
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs['headers']['Range'], 'bytes=0-3')
