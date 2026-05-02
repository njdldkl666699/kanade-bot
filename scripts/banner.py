r"""
设置前景色(38)为橙色 (RGB: 255,165,0)：
\033[38;2;255;165;0m你好

设置背景色(48)为青色 (RGB: 0,255,255)：
\033[48;2;0;255;255m

重置颜色为默认：
\033[0m

▀ (上半块, U+2580) 和 ▄ (下半块, U+2584)

如果在Windows Terminal中显示不正确，请检查对应配置文件-外观-自动调整无法区分的文本的亮度的设置
如果为“始终”，改为其他选项即可正常显示。

拼豆图纸：B站@蜂蜜酒Mead
"""

from pathlib import Path

blank = ""
white = "255;255;255"

lblue1 = "184;201;242"  # 头发边
lblue2 = "231;237;255"  # 头发整体
lblue3 = "213;222;250"  # 头发内阴影
lblue4 = "205;232;255"  # 裤子
lblue5 = "155;209;253"  # 裤子中间

yellow1 = "252;244;240"  # 皮肤
yellow2 = "253;233;229"  # 脸颊、手
yellow3 = "254;213;194"  # 脖子

dblue1 = "2;69;90"  # 眉毛
dblue2 = "40;133;204"  # 眼睛上半、衣服两点
dblue3 = "155;209;253"  # 眼睛下半
dblue4 = "29;88;146"  # 衣服内
dblue5 = "1;56;124"  # 衣服边

purple = "48;72;136"  # 手旁边

black1 = "69;67;78"  # 领口
black2 = "44;38;42"  # 鞋子


# 0, 2, 4...用前景色
# 1, 3, 5...用背景色
# 17列
# Trick: *[blank]*5 == *([blank]*5)

type Image = list[list[str]]

# fmt: off
# 21个实际行，外加1个空行，总共22行，满足偶数行的要求
KANADE_21: Image = [
    [*[blank]*5, *[lblue1]*7, *[blank]*5],
    [*[blank]*3, *[lblue1]*2, *[lblue2]*2, *[lblue3]*2, lblue2, lblue3, lblue2, *[lblue1]*2, *[blank]*3],
    [*[blank]*2, lblue1, *[lblue2]*6, lblue3, *[lblue2]*4, lblue1, *[blank]*2],
    [blank, lblue1, *[lblue2]*2, lblue3, *[lblue2]*10, lblue1, blank],
    [blank, lblue1, lblue2, lblue3, *[lblue2]*2, *[white]*3, lblue2, white, *[lblue2]*2, lblue3, lblue2, lblue1, blank],
    [lblue1, *[lblue2]*2, lblue3, *[lblue2]*10, lblue3, lblue2, lblue1],
    [lblue1, lblue2, lblue3, *[lblue2]*11, lblue3, lblue2, lblue1],
    [lblue1, *[lblue2]*6, lblue3, *[lblue2]*2, lblue3, *[lblue2]*5, lblue1],
    [lblue1, *[lblue2]*5, *[lblue3]*2, lblue2, lblue3, yellow1, lblue3, *[lblue2]*3, lblue3, lblue1],
    [lblue1, *[lblue2]*4, lblue3, yellow1, lblue3, lblue2, lblue3, *[yellow1]*2, lblue3, *[lblue2]*2, lblue3, lblue1],
    [lblue1, *[lblue2]*3, *[dblue1]*3, yellow1, *[lblue3]*2, *[dblue1]*3, lblue2, *[lblue3]*2, lblue1],
    [lblue1, lblue3, yellow2, lblue3, yellow1, *[dblue2]*2, *[yellow1]*2, lblue3, *[dblue2]*2, yellow1, lblue3, yellow2, lblue3, lblue1],
    [blank, lblue1, yellow1, lblue2, white, *[dblue3]*2, *[yellow1]*3, *[dblue3]*2, white, lblue2, yellow1, lblue1, blank],
    [*[blank]*2, lblue1, lblue2, *[yellow2]*2, *[yellow1]*5, *[yellow2]*2, lblue2, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue2, *[lblue3]*2, dblue5, dblue4, yellow3, dblue4, dblue5, *[lblue3]*2, lblue2, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue2, lblue3, dblue5, dblue4, dblue2, black1, dblue2, dblue4, dblue5, lblue3, lblue2, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue3, dblue5, *[dblue4]*7, dblue5, lblue3, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, dblue5, yellow2, purple, *[dblue4]*5, purple, yellow2, dblue5, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue3, *[dblue5]*2, *[lblue4]*2, lblue5, *[lblue4]*2, *[dblue5]*2, lblue3, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, *[lblue3]*2, dblue5, *[yellow1]*2, dblue5, *[yellow1]*2, dblue5, *[lblue3]*2, lblue1, *[blank]*2],
    [*[blank]*6, *[black2]*2, blank, *[black2]*2, *[blank]*6],
    [blank]*17,
]

# 15个实际行
KANADE_15: Image = [
    [*[blank]*5, *[lblue1]*7, *[blank]*5],
    [*[blank]*3, *[lblue1]*2, *[lblue2]*2, *[lblue3]*2, lblue2, lblue3, lblue2, *[lblue1]*2, *[blank]*3],
    [*[blank]*2, lblue1, *[lblue2]*6, lblue3, *[lblue2]*4, lblue1, *[blank]*2],
    [blank, lblue1, *[lblue2]*13, lblue1, blank],
    [blank, lblue1, *[lblue2]*2, lblue3, *[lblue2]*8, lblue3, lblue2, lblue1, blank],
    [lblue1, *[lblue2]*2, lblue3, *[lblue2]*3, lblue3, *[lblue2]*2, lblue3, *[lblue2]*5, lblue1],
    [lblue1, *[lblue2]*2, lblue3, *[lblue2]*2, *[lblue3]*2, lblue2, lblue3, yellow1, lblue3, *[lblue2]*3, lblue3, lblue1],
    [lblue1, lblue2, lblue3, *[lblue2]*2, lblue3, yellow1, lblue3, lblue2, lblue3, *[yellow1]*2, lblue3, *[lblue2]*2, lblue3, lblue1],
    [lblue1, *[lblue2]*3, *[dblue1]*3, yellow1, *[lblue3]*2, *[dblue1]*3, lblue2, *[lblue3]*2, lblue1],
    [lblue1, lblue3, yellow2, lblue3, yellow1, *[dblue2]*2, *[yellow1]*2, lblue3, *[dblue2]*2, yellow1, lblue3, yellow2, lblue3, lblue1],
    [blank, lblue1, yellow1, lblue2, white, *[dblue3]*2, *[yellow1]*3, *[dblue3]*2, white, lblue2, yellow1, lblue1, blank],
    [*[blank]*2, lblue1, lblue2, *[yellow2]*2, *[yellow1]*5, *[yellow2]*2, lblue2, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue2, *[lblue3]*2, dblue5, dblue4, yellow3, dblue4, dblue5, *[lblue3]*2, lblue2, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue2, lblue3, dblue5, dblue4, dblue2, black1, dblue2, dblue4, dblue5, lblue3, lblue2, lblue1, *[blank]*2],
    [*[blank]*2, lblue1, lblue3, dblue5, *[dblue4]*7, dblue5, lblue3, lblue1, *[blank]*2],
    [blank]*17,
]
# fmt: on


def foreground(color: str) -> str:
    return f"\033[38;2;{color}m"


def background(color: str) -> str:
    return f"\033[48;2;{color}m"


reset = "\033[0m"


def rgb_semicolon_to_css(color: str) -> str:
    return f"rgb({color.replace(';', ',')})"


def pixel2(upper: str, lower: str) -> str:
    if upper == blank and lower == blank:
        # 上下都为空白，直接返回空格
        return " " + reset
    elif upper == blank:
        # 只有下面一块，用下半块的前景色
        return foreground(lower) + "▄" + reset
    elif lower == blank:
        # 只有上面一块，用上半块的前景色
        return foreground(upper) + "▀" + reset
    else:
        # 上下都有颜色，使用上半块的前景色和下半块的背景色
        return foreground(upper) + background(lower) + "▀" + reset


def pixel2_html(upper: str, lower: str) -> str:
    if upper == blank and lower == blank:
        return "<span>&nbsp;</span>"
    if upper == blank:
        return f'<span style="color:{rgb_semicolon_to_css(lower)}">▄</span>'
    if lower == blank:
        return f'<span style="color:{rgb_semicolon_to_css(upper)}">▀</span>'
    return (
        f'<span style="color:{rgb_semicolon_to_css(upper)};'
        f'background-color:{rgb_semicolon_to_css(lower)}">▀</span>'
    )


def validate_kanade(kanade: Image) -> bool:
    # 验证 kanade 的格式是否正确
    # 每行必须有 17 列，且行数必须是偶数
    for row in kanade:
        if len(row) != 17:
            print(f"行长度不正确: {row} (长度: {len(row)})")
            return False
    if len(kanade) % 2 != 0:
        print(f"行数必须是偶数，但当前行数为: {len(kanade)}")
        return False
    return True


def get_kanade(kanade: Image = KANADE_21) -> str:
    if not validate_kanade(kanade):
        raise ValueError("KANADE 格式不正确")

    lines = []
    for i in range(0, len(kanade), 2):
        upper_row = kanade[i]
        lower_row = kanade[i + 1]
        line = "".join(pixel2(upper_row[j], lower_row[j]) for j in range(17))
        lines.append(line)
    return "\n".join(lines)


def get_kanade_html(kanade: Image = KANADE_21) -> str:
    if not validate_kanade(kanade):
        raise ValueError("KANADE 格式不正确")

    lines = []
    for i in range(0, len(kanade), 2):
        upper_row = kanade[i]
        lower_row = kanade[i + 1]
        line = "".join(pixel2_html(upper_row[j], lower_row[j]) for j in range(17))
        lines.append(line)

    html_body = "<br/>".join(lines)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kanade Banner</title>
  <style>
    body {{ margin: 24px; background: #ffffff; }}
    .kanade {{
      font-family: 'Cascadia Mono', 'Consolas', monospace;
      font-size: 1em;
      line-height: normal;
      letter-spacing: 0;
      white-space: pre;
    }}
  </style>
</head>
<body>
  <div class="kanade">{html_body}</div>
</body>
</html>
"""


if __name__ == "__main__":
    print(get_kanade(KANADE_15))
    print(len(KANADE_15))
    Path("kanade_banner.html").write_text(get_kanade_html(KANADE_15), encoding="utf-8")
