from dataclasses import dataclass

from pydantic import BaseModel, field_serializer


@dataclass
class Data:
    a: int
    b: str


class MyModel(BaseModel):
    data: Data

    @field_serializer("data", mode="plain")
    def serialize_data(self, v: Data, _info):
        return v


m = MyModel(data=Data(a=1, b="test"))
print(m.model_dump())
print(m.data)
