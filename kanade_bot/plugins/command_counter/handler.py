from datetime import timedelta

from nonebot.adapters import Message
from nonebot.params import CommandArg

from kanade_bot.utils.common import asia_shanghai_now

from .config import command_counter_data
from .matcher import top_commands


@top_commands.handle()
async def handle_top_commands(arg_msg: Message = CommandArg()):
    today = asia_shanghai_now().date()
    tomorrow = today + timedelta(days=1)
    # [start, end)  默认今天
    start_date = today
    end_date = tomorrow
    date_range_str = "今天"

    if arg := arg_msg.extract_plain_text().strip():
        if arg in ("昨天", "昨日"):
            start_date = today - timedelta(days=1)
            end_date = today
            date_range_str = "昨天"
        elif arg in ("本周", "这周"):
            start_date = today - timedelta(days=today.weekday())
            date_range_str = "本周"
        elif arg == "本月":
            start_date = today.replace(day=1)
            date_range_str = "本月"
        elif arg.isnumeric():
            # 解析为过去的N天，包含今天
            days = int(arg)
            start_date = today - timedelta(days=days - 1)
            date_range_str = f"过去{days}天"

    # 统计命令计数
    command_counts: dict[str, int] = {}
    data = command_counter_data.root
    for d in data:
        if start_date <= d < end_date:
            for command, count in data[d].items():
                command_counts[command] = command_counts.get(command, 0) + count

    if not command_counts:
        await top_commands.finish("没有命令使用记录")

    # 按计数排序，并显示前10条
    sorted_commands = sorted(command_counts.items(), key=lambda x: x[1], reverse=True)
    output = f"{date_range_str}命令使用排行："
    for command, count in sorted_commands[:10]:
        output += f"\n{command}: {count}"

    await top_commands.finish(output)
