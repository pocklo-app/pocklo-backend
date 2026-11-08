from smartdjango import Params, Validator

from Resource.models import Resource


class ResourceParams(metaclass=Params):
    model_class = Resource

    rname: Validator
    cover: Validator
    cover_type: Validator

    resource_getter = Validator('res_str_id', final_name='resource').to(Resource.get_by_id)
    parent_getter = Validator('parent_str_id', final_name='parent').to(Resource.get_by_id)
