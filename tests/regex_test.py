import re

whateat = r"[今明后]?[天日]?(?:早|中|晚)?(?:上|午|餐|饭|夜宵|宵夜|早|晚)?吃(?:什么|啥|点啥)"

test_cases = [
    "吃什么",
    "今天吃什么",
    "明天吃点啥",
    "晚饭吃什么",
    "后日中午吃什么",
    "明天夜宵吃点啥",
]


for test in test_cases:
    match = re.match(whateat, test)
    if match:
        print(f"'{test}' 匹配成功: {match.group(0)}")
    else:
        print(f"'{test}' 匹配失败")
