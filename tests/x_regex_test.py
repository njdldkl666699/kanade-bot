import re

reg = r"[x*×\s]+"

for text in ["猪", "猪x1", "猪 x 1", "猪 x1", "猪x 1", "猪 1", "猪 1x", "猪*1"]:
    result = re.split(reg, text, maxsplit=1)
    print(f"{text}: {result}")
