import re

start_regex = "|".join(map(re.escape, {"/", "#", "$"}))

# 清晰、易维护的版本
pattern = r"""
    ({start_regex})                     # 命令前缀（必须）
    (?:                                 # 时间词（可选）
        [今明后]                         # 今/明/后
        [天日]?                          # 天/日（可选）
    )?
    (?:                                 # 餐段词（可选）
        (?:早|中|晚)(?:餐|饭)?           # 早餐、中餐、晚餐、早饭等
        |早上|中午|晚上                   # 完整时间词
        |宵夜|夜宵                       # 宵夜相关
        |(?<=[今明])晚                   # 今晚、明晚
    )?
    {action}(?:什么|啥|点啥)                   # 核心动词（必须）
"""

eat_pattern = pattern.format(start_regex=start_regex, action="吃")

regex = re.compile(eat_pattern, re.VERBOSE)


# 使用
def is_eat_command(text: str) -> bool:
    """判断是否为\"吃什么\"类命令"""
    return bool(regex.match(text))


# 测试
for text in ["/今天早餐吃什么", "#明晚吃啥", "$后宵夜吃啥", "/吃什么"]:
    print(f"{text}: {is_eat_command(text)}")
