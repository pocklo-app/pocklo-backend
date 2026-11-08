from django.views import View
from smartdjango import analyse, Validator, Error

from Base.auth import Auth
from Base.common import qt_manager
from Resource.models import Resource, ResourceErrors
from User.models import User


class OAuthView(View):
    @analyse.query(Validator('code', '齐天簿授权码'))
    def get(self, request):
        code = request.query.code

        body = qt_manager.get_token(code)

        token = body['token']
        qt_user_app_id = body['user_app_id']

        user = User.create(qt_user_app_id, token)

        try:
            Resource.get_root_folder(user)
        except Error as e:
            if e == ResourceErrors.GET_ROOT_FOLDER:
                root = Resource.get_by_pk(Resource.ROOT_ID)

                Resource.create_folder(
                    user.qt_user_app_id,
                    user,
                    root,
                )

        user.refresh_profile(force=True, suppress_error=True)
        return Auth.get_login_token(user)
