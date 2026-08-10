import json
import random
from io import BytesIO
from pathlib import Path
from typing import ClassVar, Self

from PIL import Image, ImageDraw
from PIL.Image import Image as IMG
from spellchecker import SpellChecker

from kanade_bot.plugins.wordle.util import GuessResult, load_font, save_png

from .config import cfg

words_dir = cfg.dictionaries_dir_path

DIC_LIST = [f.stem for f in words_dir.iterdir() if f.suffix == ".json"]

spell = SpellChecker()


def _legal_word(word: str) -> bool:
    return not spell.unknown((word,))


Meanings = dict[str, str]
"""单词释义类型与释义的映射"""
Words = dict[str, Meanings]
"""词典数据，格式为 {单词: {释义类型: 释义}}"""


class Wordle:
    dict_words_cache: ClassVar[dict[Path, dict[int, Words]]] = {}
    """词典数据缓存，格式为 {词典路径: {单词长度: 词典数据}}"""

    letter_font = load_font("NotoSansCJK-Bold.ttc", 20)

    @classmethod
    def _load_and_cache_dict(cls, dict_name: str) -> dict[int, Words]:
        dict_path = words_dir / f"{dict_name}.json"
        if dict_path not in cls.dict_words_cache:
            # 如果缓存中没有数据，则从文件中读取数据并缓存
            json_data: Words = json.load(dict_path.open("r", encoding="utf-8"))
            cls.dict_words_cache[dict_path] = {}
            for word, info in json_data.items():
                a_word_len = len(word)
                if a_word_len not in cls.dict_words_cache[dict_path]:
                    cls.dict_words_cache[dict_path][a_word_len] = {}
                cls.dict_words_cache[dict_path][a_word_len][word] = info
        return cls.dict_words_cache[dict_path]

    @classmethod
    def random_wordle(cls, dict_name: str = "CET4", word_length: int = 5) -> Self:
        data = cls._load_and_cache_dict(dict_name)
        word, meanings = random.choice(list(data[word_length].items()))
        return cls(word, meanings)

    @classmethod
    def count_words_by_length(cls, dict_name: str = "CET4", length: int = 5) -> int:
        data = cls._load_and_cache_dict(dict_name)
        return len(data[length])

    def __init__(self, word: str, meanings: Meanings):
        """
        :param word: 单词
        :param meanings: 单词释义类型与释义的映射
        """
        self.result: str = f"【单词】：{word}"
        for meaning in cfg.meanings:
            if meaning in meanings:
                self.result += f"\n【{meaning}】：{meanings[meaning]}"

        self.word_lower: str = word.lower()
        self.length: int = len(word)
        """单词长度"""
        self.rows: int = 6
        """可猜次数"""
        self.guessed_words: list[str] = []
        """记录已猜单词"""
        self.crystal_bonus: int = cfg.crystal_bonus_map.get(self.length, 0)
        """水晶奖励"""

        self.block_size = (40, 40)
        """文字块尺寸"""
        self.block_padding = (10, 10)
        """文字块之间间距"""
        self.padding = (20, 20)
        """边界间距"""
        self.border_width = 2
        """边框宽度"""
        self.font = self.letter_font

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

    def set_hinted_crystal_bonus(self):
        """设置使用提示后猜出单词的水晶奖励"""
        if self.length in cfg.hinted_crystal_bonus_map:
            self.crystal_bonus = cfg.hinted_crystal_bonus_map[self.length]
        # 如果未配置使用提示后的水晶奖励，则保持原来的奖励不变
