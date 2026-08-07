from collections.abc import Callable

from pydantic import BaseModel, create_model


class AttrDocModel(BaseModel):
    model_config = {"use_attribute_docstrings": True}


class A(AttrDocModel):
    attr_a: str = "A_test"
    """A的属性a的文档字符串"""


class B(AttrDocModel):
    attr_b: str = "B_test"
    """B的属性b的文档字符串"""


class Config(BaseModel):
    a: A = A()
    b: B = B()


class CallableModel(BaseModel):
    test: Callable[[], str] = lambda: "test"


def merge_models(*models: type[BaseModel], name: str = "Merged") -> type[BaseModel]:
    fields = {}
    for m in models:
        for field_name, field_info in m.model_fields.items():
            fields[field_name] = (field_info.annotation, field_info)
    return create_model(name, __config__=AttrDocModel.model_config, **fields)


Merged = merge_models(Config, name="FlatCombined")
print(Merged.model_json_schema())
m = CallableModel()
m.test()
