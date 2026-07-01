from nonebot.adapters.onebot.v11 import Message

message = Message("消息")
for i, segment in enumerate(message):
    print(f"Segment {i}: type={segment.type}, data={segment.data}")
    if segment.type == "text":
        print(f"Text content: {segment.data['text']}")

list_messages: list[str] = ["1", "2"]
for m in list_messages:
    Message()
