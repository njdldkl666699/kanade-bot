from nonebot.adapters import Event, Message
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg, EventPlainText
from nonebot.typing import T_State

from kanade_bot.plugins.api60s.client import client
from kanade_bot.utils.parse import parse_arg_message

from .matcher import fanyi, moyu, weather, weather_forecast
from .translation import process_translation


@weather.handle()
async def handle_weather(args: Message = CommandArg()):
    response = await client.get(
        "/v2/weather",
        params={
            "query": args.extract_plain_text(),
            "encoding": "text",
        },
    )
    await weather.finish(response.text)


@weather_forecast.handle()
async def handle_weather_forecast(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"query": str, "days": int})
    response = await client.get(
        "/v2/weather/forecast",
        params={
            "query": args["query"],
            "days": args["days"],
            "encoding": "text",
        },
    )
    await weather_forecast.finish(response.text)


@fanyi.handle()
async def handle_fanyi(state: T_State, event: Event, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"to_query": str, "from_query": str})
    to_query, from_query = args["to_query"], args["from_query"]

    if not isinstance(event, OneBotMessageEvent) or not event.reply:
        state["from_query"] = from_query
        state["to_query"] = to_query
        await fanyi.pause("请发送一条消息以进行翻译。")

    # 回复消息中的文本作为要翻译的内容
    text = event.reply.message.extract_plain_text()
    message = await process_translation(text, from_query, to_query)
    await fanyi.finish(message)


@fanyi.handle()
async def handle_fanyi_text(state: T_State, message: str = EventPlainText()):
    from_query = state["from_query"]
    to_query = state["to_query"]
    response = await process_translation(message, from_query, to_query)
    await fanyi.finish(response)


@moyu.handle()
async def handle_moyu():
    response = await client.get("/v2/moyu", params={"encoding": "text"})
    await moyu.finish(response.text)
