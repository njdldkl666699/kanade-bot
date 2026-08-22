import json

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.compat import model_dump
from nonebot.utils import DataclassEncoder

# message = Message("消息")
# for i, segment in enumerate(message):
#     print(f"Segment {i}: type={segment.type}, data={segment.data}")
#     if segment.type == "text":
#         print(f"Text content: {segment.data['text']}")

node_custom_message = Message()
list_messages: list[str] = ["1", "2"]
for segment in list_messages:
    node_custom_message += MessageSegment.node_custom(1234567, "昵称", Message(segment))
result_message = Message()
result_message += node_custom_message
print(json.dumps(result_message, cls=DataclassEncoder, ensure_ascii=False, indent=2))
