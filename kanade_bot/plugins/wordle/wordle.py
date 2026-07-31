import json
import random
from enum import Enum
from io import BytesIO
from pathlib import Path
from typing import ClassVar, Self

from nonebot import require
from PIL import Image, ImageDraw, ImageFont
from PIL.Image import Image as IMG
from spellchecker import SpellChecker

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_dir

words_dir = get_plugin_config_dir()

DIC_LIST = [f.stem for f in words_dir.iterdir() if f.suffix == ".json"]

spell = SpellChecker()


def _legal_word(word: str) -> bool:
    return not spell.unknown((word,))


def save_png(frame: IMG) -> BytesIO:
    output = BytesIO()
    frame = frame.convert("RGBA")
    frame.save(output, format="png")
    return output


def _load_font(size: int, *, bold=True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_names = (
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "NotoSansCJK-Bold.ttc",
        )
        if bold
        else (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "NotoSansCJK-Regular.ttc",
        )
    )
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


class GuessResult(Enum):
    WIN = 0
    """猜出正确单词"""
    LOSS = 1
    """达到最大可猜次数，未猜出正确单词"""
    DUPLICATE = 2
    """单词重复"""
    ILLEGAL = 3
    """单词不合法"""


class Wordle:
    dict_words_cache: ClassVar[dict[Path, dict[int, list[tuple[str, str]]]]] = {}
    """词典数据缓存，格式为 {词典路径: {单词长度: [(单词, 释义)]}}"""

    @classmethod
    def random_wordle(cls, dict_name: str = "CET4", word_length: int = 5) -> Self:
        caches = cls.dict_words_cache
        dict_path = words_dir / f"{dict_name}.json"
        if dict_path not in caches:
            # 如果缓存中没有数据，则从文件中读取数据并缓存
            json_data = json.load(dict_path.open("r", encoding="utf-8"))
            caches[dict_path] = {}
            for word, info in json_data.items():
                a_word_len = len(word)
                if a_word_len not in caches[dict_path]:
                    caches[dict_path][a_word_len] = []
                caches[dict_path][a_word_len].append((word, info["中释"]))

        # 从缓存中获取随机单词
        data = caches[dict_path]
        word, meaning = random.choice(data[word_length])
        return cls(word, meaning)

    def __init__(self, word: str, meaning: str):
        self.word: str = word
        """单词"""
        self.meaning: str = meaning
        """单词释义"""
        self.result = f"【单词】：{self.word}\n【释义】：{self.meaning}"
        self.word_lower: str = self.word.lower()
        self.length: int = len(word)
        """单词长度"""
        self.rows: int = self.length + 1
        """可猜次数"""
        self.guessed_words: list[str] = []
        """记录已猜单词"""

        self.block_size = (40, 40)
        """文字块尺寸"""
        self.block_padding = (10, 10)
        """文字块之间间距"""
        self.padding = (20, 20)
        """边界间距"""
        self.border_width = 2
        """边框宽度"""
        self.font_size = 20
        """字体大小"""
        self.font = _load_font(self.font_size, bold=True)

        self.correct_color = (134, 163, 115)
        """存在且位置正确时的颜色"""
        self.exist_color = (198, 182, 109)
        """存在但位置不正确时的颜色"""
        self.wrong_color = (123, 123, 124)
        """不存在时颜色"""
        self.border_color = (123, 123, 124)
        """边框颜色"""
        self.bg_color = (255, 255, 255)
        """背景颜色"""
        self.font_color = (255, 255, 255)
        """文字颜色"""

    def guess(self, word: str) -> GuessResult | None:
        word = word.lower()
        if word == self.word_lower:
            self.guessed_words.append(word)
            return GuessResult.WIN
        if word in self.guessed_words:
            return GuessResult.DUPLICATE
        if not _legal_word(word):
            return GuessResult.ILLEGAL
        self.guessed_words.append(word)
        if len(self.guessed_words) == self.rows:
            return GuessResult.LOSS

    def draw_block(self, color: tuple[int, int, int], letter: str) -> IMG:
        block = Image.new("RGB", self.block_size, self.border_color)
        inner_w = self.block_size[0] - self.border_width * 2
        inner_h = self.block_size[1] - self.border_width * 2
        inner = Image.new("RGB", (inner_w, inner_h), color)
        block.paste(inner, (self.border_width, self.border_width))

        if letter:
            letter = letter.upper()
            draw = ImageDraw.Draw(block)
            # 使用 anchor='mm' 实现水平和垂直居中
            draw.text(
                (self.block_size[0] / 2, self.block_size[1] / 2),
                letter,
                font=self.font,
                fill=self.font_color,
                anchor="mm",  # 'm' = middle (both horizontal and vertical)
            )
        return block

    def draw(self) -> BytesIO:
        board_w = self.length * self.block_size[0]
        board_w += (self.length - 1) * self.block_padding[0] + 2 * self.padding[0]
        board_h = self.rows * self.block_size[1]
        board_h += (self.rows - 1) * self.block_padding[1] + 2 * self.padding[1]
        board_size = (board_w, board_h)
        board = Image.new("RGB", board_size, self.bg_color)

        for row in range(self.rows):
            if row < len(self.guessed_words):
                guessed_word = self.guessed_words[row]

                word_incorrect = ""  # 猜错的字母
                for i in range(self.length):
                    if guessed_word[i] != self.word_lower[i]:
                        word_incorrect += self.word_lower[i]
                    else:
                        word_incorrect += "_"  # 猜对的字母用下划线代替

                blocks: list[IMG] = []
                for i in range(self.length):
                    letter = guessed_word[i]
                    if letter == self.word_lower[i]:
                        color = self.correct_color
                    elif letter in word_incorrect:
                        """
                        一个字母的黄色和绿色数量与答案中的数量保持一致
                        以输入apple，答案adapt为例
                        结果为apple的第一个p是黄色，第二个p是灰色
                        代表答案中只有一个p，且不在第二个位置
                        """
                        word_incorrect = word_incorrect.replace(letter, "_", 1)
                        color = self.exist_color
                    else:
                        color = self.wrong_color
                    blocks.append(self.draw_block(color, letter))

            else:
                blocks = [self.draw_block(self.bg_color, "") for _ in range(self.length)]

            for col, block in enumerate(blocks):
                x = self.padding[0] + (self.block_size[0] + self.block_padding[0]) * col
                y = self.padding[1] + (self.block_size[1] + self.block_padding[1]) * row
                board.paste(block, (x, y))
        return save_png(board)

    def get_hint(self) -> str:
        letters = set()
        for word in self.guessed_words:
            for letter in word:
                if letter in self.word_lower:
                    letters.add(letter)
        return "".join([i if i in letters else "*" for i in self.word_lower])

    def draw_hint(self, hint: str) -> BytesIO:
        board_w = self.length * self.block_size[0]
        board_w += (self.length - 1) * self.block_padding[0] + 2 * self.padding[0]
        board_h = self.block_size[1] + 2 * self.padding[1]
        board = Image.new("RGB", (board_w, board_h), self.bg_color)

        for i in range(len(hint)):
            letter = hint[i].replace("*", "")
            color = self.correct_color if letter else self.bg_color
            x = self.padding[0] + (self.block_size[0] + self.block_padding[0]) * i
            y = self.padding[1]
            board.paste(self.draw_block(color, letter), (x, y))
        return save_png(board)
