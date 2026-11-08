from smartdjango import Error, Code, Choice


@Error.register
class ResourceErrors:
    PARENT_NOT_BELONG = Error("无法在他人目录下存储", code=Code.Forbidden)
    NOT_ALLOWED_COVER_SELF_OF_NOT_IMAGE = Error("不允许非图片资源设置封面为自身", code=Code.Forbidden)
    NOT_ALLOWED_COVER_UPLOAD = Error("不允许封面类型直接修改为本地上传类型", code=Code.Forbidden)

    DELETE_ROOT_FOLDER = Error("无法删除用户根目录", code=Code.Forbidden)
    RESOURCE_CIRCLE = Error("不允许资源关系成环", code=Code.Forbidden)

    REQUIRE_FOLDER = Error("目标需要为目录资源", code=Code.Forbidden)
    REQUIRE_FILE = Error("目标需要为文件资源", code=Code.Forbidden)

    NOT_READABLE = Error("没有读取权限", code=Code.Forbidden)
    RIGHT_NOT_FOUND = Error("不存在的读取权限", code=Code.NotFound)
    CREATE_RIGHT = Error("存储读取权限错误", code=Code.InternalServerError)
    REQUIRE_EMPTY_FOLDER = Error("非空目录无法删除", code=Code.Forbidden)
    GET_ROOT_FOLDER = Error("无法获得根目录", code=Code.InternalServerError)
    CREATE_LINK = Error("存储链接错误", code=Code.InternalServerError)
    CREATE_FOLDER = Error("存储目录错误", code=Code.InternalServerError)
    CREATE_FILE = Error("存储文件错误", code=Code.InternalServerError)
    FILE_PARENT = Error("文件资源不能成为父目录", code=Code.Forbidden)
    RESOURCE_NOT_BELONG = Error("没有权限", code=Code.Forbidden)
    RESOURCE_NOT_FOUND = Error("不存在的资源", code=Code.NotFound)
    INVALID_RNAME = Error("不合法的资源名称", code=Code.BadRequest)

    VISIT_KEY_TOO_SHORT = Error("访问密钥长度不能小于 {length}", code=Code.BadRequest)


class RtypeChoice(Choice):
    FILE = 0
    FOLDER = 1
    LINK = 2


class StypeChoice(Choice):
    FOLDER = 0
    IMAGE = 1
    VIDEO = 2
    MUSIC = 3
    FILE = 4
    LINK = 5


class CoverChoice(Choice):
    RANDOM = 0
    UPLOAD = 1
    PARENT = 2
    OUTLNK = 3
    SELF = 4
    RESOURCE = 5


class ResourceValidator:
    MAX_RNAME_LENGTH = 256
    MAX_MIME_LENGTH = 100
    MAX_COVER_LENGTH = 1024
    MAX_DLPATH_LENGTH = 1024
    MAX_VISIT_KEY_LENGTH = 16
    MIN_VISIT_KEY_LENGTH = 3
    MAX_RES_STR_ID_LENGTH = 6

    @classmethod
    def rname(cls, rname):
        """验证rname属性"""
        invalid_chars = '\\/*:\'"|<>?'
        for char in invalid_chars:
            if char in rname:
                raise ResourceErrors.INVALID_RNAME

    @staticmethod
    def parent(parent):
        """验证parent属性"""
        # if not isinstance(parent, Resource):
        #     raise BaseError.STRANGE
        if parent.rtype != RtypeChoice.FOLDER:
            raise ResourceErrors.FILE_PARENT

    @staticmethod
    def visit_key(visit_key):
        """验证visit_key属性"""
        if len(visit_key) < ResourceValidator.MIN_VISIT_KEY_LENGTH:
            raise ResourceErrors.VISIT_KEY_TOO_SHORT(length=ResourceValidator.MIN_VISIT_KEY_LENGTH)
