from functools import wraps

from smartdjango import Error, Code, analyse
from smartdjango.analyse import Request

from Base.common import ADMIN_QITIAN
from Base.jtoken import JWT

from Resource.models import ResourceErrors


@Error.register
class AuthErrors:
    REQUIRE_ROOT = Error("需要管理员权限", code=Code.Unauthorized)
    REQUIRE_RIGHT = Error("需要{0}权限", code=Code.Unauthorized)
    EXPIRED = Error("登录过期", code=Code.Unauthorized)
    REQUIRE_ADMIN = Error("需要管理员登录", code=Code.Unauthorized)
    REQUIRE_USER = Error("需要登录", code=Code.Unauthorized)
    TOKEN_MISS_PARAM = Error("认证口令缺少参数{0}", code=Code.BadRequest)
    REQUIRE_LOGIN = Error("需要登录", code=Code.Unauthorized)


class Auth:
    @staticmethod
    def _parse_token(request: Request):
        jwt_str = request.META.get('HTTP_TOKEN')
        if jwt_str is None:
            raise AuthErrors.REQUIRE_LOGIN
        return JWT.decrypt(jwt_str)

    @staticmethod
    def get_login_token(user):
        token, dict_ = JWT.encrypt(dict(
            user_id=user.pk,
        ), expire_second=30 * 60 * 60 * 24)
        dict_['token'] = token
        dict_['user'] = user.d()
        return dict_

    @classmethod
    def _extract_user(cls, request):
        request.user = None

        dict_ = Auth._parse_token(request)

        user_id = dict_.get('user_id')
        if not user_id:
            raise AuthErrors.TOKEN_MISS_PARAM('user_id')

        from User.models import User
        request.user = User.get_by_id(user_id)

    @staticmethod
    def maybe_login(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            request: Request = analyse.get_request(*args)
            try:
                Auth._extract_user(request)
            except Error:
                pass
            return func(*args, **kwargs)

        return wrapper

    @classmethod
    def require_login(cls, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            request: Request = analyse.get_request(*args)
            cls._extract_user(request)
            return func(*args, **kwargs)

        return wrapper

    @classmethod
    def require_owner(cls, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            request: Request = analyse.get_request(*args)
            cls._extract_user(request)
            if not request.data.resource.belong(request.user):
                return ResourceErrors.RESOURCE_NOT_BELONG
            return func(*args, **kwargs)
        return wrapper

    @classmethod
    def require_root(cls, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            request = analyse.get_request(*args)
            cls._extract_user(request)
            user = request.user
            if not ADMIN_QITIAN or user.qt_user_app_id != ADMIN_QITIAN:
                raise AuthErrors.REQUIRE_ROOT
            return func(*args, **kwargs)
        return wrapper
