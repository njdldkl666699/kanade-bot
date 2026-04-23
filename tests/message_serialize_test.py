from pathlib import Path

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from pydantic import BaseModel, RootModel


class MyModel(RootModel[dict[int, Message]]):
    pass


if __name__ == "__main__":
    segment = MessageSegment.image(Path("assets/images/grass_block.png"))
    message = Message(segment)
    model = MyModel({123: message})
    json = model.model_dump_json()
    message2 = MyModel.model_validate_json(json).root[123]
    print("Original Message:", message)
    print("Serialized JSON:", json)
    print("Deserialized Message:", message2)
    print("Equality Check:", message == message2)
