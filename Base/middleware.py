from django.http import HttpResponse, HttpResponseBase

from smartdjango.error import Error, OK
from smartdjango.utils import io


class CompatibleAPIPacker:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request, *args, **kwargs):
        response = self.get_response(request, *args, **kwargs)
        if isinstance(response, HttpResponseBase):
            return response

        return self.pack(response)

    @classmethod
    def process_exception(cls, _, error):
        if isinstance(error, Error):
            return cls.pack(error)

    @staticmethod
    def pack(response):
        if isinstance(response, Error):
            body, error = None, response
        else:
            body, error = response, OK

        response = error.json()
        response['body'] = body
        response['code'] = int(error.identifier != OK.identifier)
        response['msg'] = error.user_message
        response['append_msg'] = getattr(error, 'append_msg', '')
        response['debug_msg'] = error.details
        response.pop('message')
        response.pop('details')

        response = io.json_dumps(response, indent=False)
        response = HttpResponse(
            response,
            status=error.code,
            content_type="application/json; encoding=utf-8",
        )
        return response
