from markdown_it import MarkdownIt

# 使用 CommonMark 规范
md = MarkdownIt("commonmark")

# 纯文本段落中允许出现的 block token
ALLOWED_BLOCK_TOKENS = {
    "paragraph_open",
    "paragraph_close",
    "inline",
}

# 纯文本中允许出现的 inline token
ALLOWED_INLINE_TOKENS = {
    "text",
    "softbreak",
    "hardbreak",
}


def guess_format(text: str) -> str:
    """
    启发式判断：返回 'markdown' 或 'text'。

    判断依据：是否出现了 Markdown 特有的语法结构。
    注意：如果纯文本中恰好写了 **粗体**，它也会被判定为 markdown，
    因为从语法上讲它就是 Markdown。
    """
    tokens = md.parse(text)

    for token in tokens:
        # 检查块级 token
        if token.type not in ALLOWED_BLOCK_TOKENS:
            return "markdown"

        # 检查行内 token
        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type not in ALLOWED_INLINE_TOKENS:
                    return "markdown"

    return "text"


def analyze_format(text: str):
    """
    返回 (格式, 原因列表)，方便调试。
    """
    tokens = md.parse(text)
    reasons = []

    for token in tokens:
        if token.type not in ALLOWED_BLOCK_TOKENS:
            reasons.append(f"block:{token.type}")

        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type not in ALLOWED_INLINE_TOKENS:
                    reasons.append(f"inline:{child.type}")

    fmt = "markdown" if reasons else "text"
    return fmt, reasons


if __name__ == "__main__":
    sample_markdown = """（重新握起笔，这回把"偶像与粉丝"的距离好好摆正了）

---

**XXXX 15:00**
"""

    sample_text = """（从屏幕前抬起头，轻轻应了一声）

……中午好。今天太阳还是很大呢，可乐也要注意别中暑哦。
"""

    print("sample_markdown ->", guess_format(sample_markdown))
    print("sample_text     ->", guess_format(sample_text))

    print("\n--- 详细分析 ---")
    for name, content in [
        ("sample_markdown", sample_markdown),
        ("sample_text", sample_text),
    ]:
        fmt, reasons = analyze_format(content)
        print(f"{name}: {fmt}")
        if reasons:
            print("  触发原因:", ", ".join(reasons))
