import json
import random
from dataclasses import dataclass
from enum import Enum
from io import BytesIO
from typing import Self

from nonebot import get_driver
from PIL import Image, ImageDraw
from PIL.Image import Image as IMG
from pypinyin import Style, pinyin

from kanade_bot.plugins.wordle.util import GuessResult, load_font, save_png

from .config import cfg

answers: list[dict[str, str]] = []

idioms: list[str] = []


@get_driver().on_startup
def _():
    global answers, idioms
    with cfg.answer_file_path.open("r", encoding="utf-8") as f:
        answers = json.load(f)
    if cfg.strict_mode:
        with cfg.idiom_file_path.open("r", encoding="utf-8") as f:
            idioms = [idiom.strip() for idiom in f.readlines()]


def _legal_idiom(word: str) -> bool:
    return word in idioms


# fmt: off
# 声母
INITIALS = [
    "zh", "z", "y", "x", "w", "t", "sh", "s", "r", "q", "p",
    "n", "m", "l", "k", "j", "h", "g", "f", "d", "ch", "c", "b"
]
# 韵母
FINALS = [
    "ün", "üe", "üan", "ü", "uo", "un", "ui", "ue", "uang",
    "uan", "uai","ua", "ou", "iu", "iong", "ong", "io", "ing",
    "in", "ie", "iao", "iang", "ian", "ia", "er", "eng", "en",
    "ei", "ao", "ang", "an", "ai", "u", "o", "i", "e", "a"
]
# fmt: on


def get_pinyin(idiom: str) -> list[tuple[str, str, str]]:
    pys = pinyin(idiom, style=Style.TONE3, v_to_u=True)
    results = []
    for p in pys:
        py = p[0]
        if py[-1].isdigit():
            tone = py[-1]
            py = py[:-1]
        else:
            tone = ""
        initial = ""
        for i in INITIALS:
            if py.startswith(i):
                initial = i
                break
        final = ""
        for f in FINALS:
            if py.endswith(f):
                final = f
                break
        results.append((initial, final, tone))  # 声母，韵母，声调
    return results


class GuessState(Enum):
    CORRECT = 0  # 存在且位置正确
    EXIST = 1  # 存在但位置不正确
    WRONG = 2  # 不存在


@dataclass
class ColorGroup:
    bg_color: str  # 背景颜色
    block_color: str  # 方块颜色
    correct_color: str  # 存在且位置正确时的颜色
    exist_color: str  # 存在但位置不正确时的颜色
    wrong_color_pinyin: str  # 不存在时的颜色
    wrong_color_char: str  # 不存在时的颜色


NORMAL_COLOR = ColorGroup("#ffffff", "#f7f8f9", "#1d9c9c", "#de7525", "#b4b8be", "#5d6673")

ENHANCED_COLOR = ColorGroup("#ffffff", "#f7f8f9", "#5ba554", "#ff46ff", "#b4b8be", "#5d6673")


class Handle:
    font_char = load_font("NotoSansSC-Regular.otf", 60)
    """汉字字体"""
    font_pinyin = load_font("NotoSansMono-Regular.ttf", 30)
    """拼音字体"""
    font_tone = load_font("NotoSansMono-Regular.ttf", 22)
    """声调字体"""

    @classmethod
    def random_handle(cls, *, strict=False) -> Self:
        answer = random.choice(answers)
        return cls(answer["word"], answer["explanation"], strict=strict)

    def __init__(self, idiom: str, explanation: str, *, strict: bool = False):
        self.idiom: str = idiom  # 成语
        self.explanation: str = explanation  # 释义
        self.strict: bool = strict  # 是否判断输入词语为成语
        self.result = f"【成语】：{idiom}\n【释义】：{explanation}"
        self.pinyin: list[tuple[str, str, str]] = get_pinyin(idiom)  # 拼音
        self.length = 4
        self.times: int = 10  # 可猜次数
        self.guessed_idiom: list[str] = []  # 记录已猜成语
        self.guessed_pinyin: list[list[tuple[str, str, str]]] = []  # 记录已猜成语的拼音

        self.block_size = (160, 160)  # 文字块尺寸
        self.block_padding = (20, 20)  # 文字块之间间距
        self.padding = (40, 40)  # 边界间距

        self.colors = ENHANCED_COLOR if cfg.color_enhance else NORMAL_COLOR

    def guess(self, idiom: str) -> GuessResult | None:
        if self.strict and not _legal_idiom(idiom):
            return GuessResult.ILLEGAL
        if idiom in self.guessed_idiom:
            return GuessResult.DUPLICATE
        self.guessed_idiom.append(idiom)
        self.guessed_pinyin.append(get_pinyin(idiom))
        if idiom == self.idiom:
            return GuessResult.WIN
        if len(self.guessed_idiom) == self.times:
            return GuessResult.LOSS

    def draw_block(
        self,
        block_color: str,
        char: str = "",
        char_color: str = "",
        initial: str = "",
        initial_color: str = "",
        final: str = "",
        final_color: str = "",
        tone: str = "",
        tone_color: str = "",
        underline: bool = False,
        underline_color: str = "",
    ) -> IMG:
        block = Image.new("RGB", self.block_size, block_color)
        if not char:
            return block
        draw = ImageDraw.Draw(block)

        char_size = self.font_char.getbbox(char)[2:]
        x = (self.block_size[0] - char_size[0]) / 2
        y = (self.block_size[1] - char_size[1]) / 5 * 3
        draw.text((x, y), char, font=self.font_char, fill=char_color)

        space = 5
        need_space = bool(initial and final)
        py_length = self.font_pinyin.getlength(initial + final)
        if need_space:
            py_length += space
        py_start = (self.block_size[0] - py_length) / 2
        x = py_start
        y = self.block_size[0] / 8
        draw.text((x, y), initial, font=self.font_pinyin, fill=initial_color)
        x += self.font_pinyin.getlength(initial)
        if need_space:
            x += space
        draw.text((x, y), final, font=self.font_pinyin, fill=final_color)

        tone_size = self.font_tone.getbbox(tone)[2:]
        x = (self.block_size[0] + py_length) / 2 + tone_size[0] / 3
        y -= tone_size[1] / 3
        draw.text((x, y), tone, font=self.font_tone, fill=tone_color)

        if underline:
            x = py_start
            py_size = self.font_pinyin.getbbox(initial + final)[2:]
            y = self.block_size[0] / 8 + py_size[1] + 2
            draw.line((x, y, x + py_length, y), fill=underline_color, width=1)
            y += 3
            draw.line((x, y, x + py_length, y), fill=underline_color, width=1)

        return block

    def draw(self) -> BytesIO:
        rows = min(len(self.guessed_idiom) + 1, self.times)
        board_w = self.length * self.block_size[0]
        board_w += (self.length - 1) * self.block_padding[0] + 2 * self.padding[0]
        board_h = rows * self.block_size[1]
        board_h += (rows - 1) * self.block_padding[1] + 2 * self.padding[1]
        board_size = (board_w, board_h)
        board = Image.new("RGB", board_size, self.colors.bg_color)

        def get_states(guessed: list[str], answer: list[str]) -> list[GuessState]:
            states = []
            incorrect = []
            for i in range(self.length):
                if guessed[i] != answer[i]:
                    incorrect.append(answer[i])
                else:
                    incorrect.append("_")
            for i in range(self.length):
                if guessed[i] == answer[i]:
                    states.append(GuessState.CORRECT)
                elif guessed[i] in incorrect:
                    states.append(GuessState.EXIST)
                    incorrect[incorrect.index(guessed[i])] = "_"
                else:
                    states.append(GuessState.WRONG)
            return states

        def get_pinyin_color(state: GuessState) -> str:
            if state == GuessState.CORRECT:
                return self.colors.correct_color
            elif state == GuessState.EXIST:
                return self.colors.exist_color
            else:
                return self.colors.wrong_color_pinyin

        def get_char_color(state: GuessState) -> str:
            if state == GuessState.CORRECT:
                return self.colors.correct_color
            elif state == GuessState.EXIST:
                return self.colors.exist_color
            else:
                return self.colors.wrong_color_char

        def block_pos(row: int, col: int) -> tuple[int, int]:
            x = self.padding[0] + (self.block_size[0] + self.block_padding[0]) * col
            y = self.padding[1] + (self.block_size[1] + self.block_padding[1]) * row
            return x, y

        for i in range(len(self.guessed_idiom)):
            idiom = self.guessed_idiom[i]
            pinyin = self.guessed_pinyin[i]
            char_states = get_states(list(idiom), list(self.idiom))
            initial_states = get_states([p[0] for p in pinyin], [p[0] for p in self.pinyin])
            final_states = get_states([p[1] for p in pinyin], [p[1] for p in self.pinyin])
            tone_states = get_states([p[2] for p in pinyin], [p[2] for p in self.pinyin])
            underline_states = get_states(
                [p[0] + p[1] for p in pinyin], [p[0] + p[1] for p in self.pinyin]
            )
            for j in range(self.length):
                char = idiom[j]
                i2, f2, t2 = pinyin[j]
                if char == self.idiom[j]:
                    block_color = self.colors.correct_color
                    char_color = initial_color = final_color = tone_color = self.colors.bg_color
                    underline = False
                    underline_color = ""
                else:
                    block_color = self.colors.block_color
                    char_color = get_char_color(char_states[j])
                    initial_color = get_pinyin_color(initial_states[j])
                    final_color = get_pinyin_color(final_states[j])
                    tone_color = get_pinyin_color(tone_states[j])
                    underline_color = get_pinyin_color(underline_states[j])
                    underline = underline_color in (
                        self.colors.correct_color,
                        self.colors.exist_color,
                    )
                block = self.draw_block(
                    block_color,
                    char,
                    char_color,
                    i2,
                    initial_color,
                    f2,
                    final_color,
                    t2,
                    tone_color,
                    underline,
                    underline_color,
                )
                board.paste(block, block_pos(i, j))

        for i in range(len(self.guessed_idiom), rows):
            for j in range(self.length):
                block = self.draw_block(self.colors.block_color)
                board.paste(block, block_pos(i, j))

        return save_png(board)

    def draw_hint(self) -> BytesIO:
        guessed_char = set("".join(self.guessed_idiom))
        guessed_initial = set()
        guessed_final = set()
        guessed_tone = set()
        for a_pinyin in self.guessed_pinyin:
            for p in a_pinyin:
                guessed_initial.add(p[0])
                guessed_final.add(p[1])
                guessed_tone.add(p[2])

        board_w = self.length * self.block_size[0]
        board_w += (self.length - 1) * self.block_padding[0] + 2 * self.padding[0]
        board_h = self.block_size[1] + 2 * self.padding[1]
        board = Image.new("RGB", (board_w, board_h), self.colors.bg_color)

        for i in range(self.length):
            char = self.idiom[i]
            hi, hf, ht = self.pinyin[i]
            color = char_c = initial_c = final_c = tone_c = self.colors.correct_color
            if char not in guessed_char:
                char = "?"
                color = self.colors.block_color
                char_c = self.colors.wrong_color_char
            else:
                char_c = initial_c = final_c = tone_c = self.colors.bg_color
            if hi not in guessed_initial:
                hi = "?"
                initial_c = self.colors.wrong_color_pinyin
            if hf not in guessed_final:
                hf = "?"
                final_c = self.colors.wrong_color_pinyin
            if ht not in guessed_tone:
                ht = "?"
                tone_c = self.colors.wrong_color_pinyin
            block = self.draw_block(color, char, char_c, hi, initial_c, hf, final_c, ht, tone_c)
            x = self.padding[0] + (self.block_size[0] + self.block_padding[0]) * i
            y = self.padding[1]
            board.paste(block, (x, y))
        return save_png(board)
