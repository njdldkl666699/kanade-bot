import json
import random
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Self

from PIL import Image, ImageDraw

from kanade_bot.plugins.wordle.util import GuessResult, save_png

from .config import cfg

SUPPORTED_RECIPE_TYPES = {
    "minecraft:crafting_shaped",
    "minecraft:crafting_shapeless",
}


class MindleDataError(RuntimeError):
    pass


class GuessState(Enum):
    CORRECT = 0
    PRESENT = 1
    WRONG = 2


@dataclass(frozen=True)
class Ingredient:
    options: tuple[str, ...] = ()

    @property
    def is_air(self) -> bool:
        return not self.options

    def matches(self, other: "Ingredient") -> bool:
        if self.is_air or other.is_air:
            return self.is_air and other.is_air
        return not set(self.options).isdisjoint(other.options)


AIR = Ingredient()


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    result_id: str
    result_name: str
    ingredients: tuple[Ingredient, ...]


@dataclass(frozen=True)
class RecipeHint:
    item_id: str
    item_name: str
    image_path: Path


def _strip_namespace(item_id: str) -> str:
    return item_id.split(":", 1)[-1]


def _ingredient_from_json(value: Any) -> Ingredient:
    if isinstance(value, str):
        # Prepared data expands tags. Keeping the tag id as a fallback makes
        # unprepared modern recipe data fail visibly instead of crashing.
        return Ingredient((_strip_namespace(value.lstrip("#")),))
    if isinstance(value, list):
        options: list[str] = []
        for item in value:
            options.extend(_ingredient_from_json(item).options)
        return Ingredient(tuple(dict.fromkeys(options)))
    if isinstance(value, dict):
        if "item" in value:
            return _ingredient_from_json(value["item"])
        if "tag" in value:
            return _ingredient_from_json(f"#{value['tag']}")
    raise ValueError(f"不支持的原料格式：{value!r}")


def _centered_offset(size: int) -> int:
    return (3 - size) // 2


def parse_recipe(path: Path, translations: dict[str, str]) -> Recipe | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    recipe_type = data.get("type")
    if recipe_type not in SUPPORTED_RECIPE_TYPES:
        return None

    result = data.get("result")
    result_id = result.get("id") if isinstance(result, dict) else result
    if not isinstance(result_id, str):
        raise TypeError("配方缺少 result.id")
    result_id = _strip_namespace(result_id)
    result_name = translations.get(result_id)
    if not result_name:
        return None

    slots = [AIR] * 9
    if recipe_type == "minecraft:crafting_shaped":
        pattern = data.get("pattern", [])
        key = data.get("key", {})
        if not pattern or len(pattern) > 3 or any(len(row) > 3 for row in pattern):
            raise ValueError("有序配方 pattern 必须在 3x3 范围内")
        row_offset = _centered_offset(len(pattern))
        col_offset = _centered_offset(max(map(len, pattern)))
        for row_index, row in enumerate(pattern):
            for col_index, symbol in enumerate(row):
                if symbol != " ":
                    slots[(row_index + row_offset) * 3 + col_index + col_offset] = (
                        _ingredient_from_json(key[symbol])
                    )
    else:
        ingredients = data.get("ingredients", [])
        if len(ingredients) > 9:
            raise ValueError("无序配方原料数不能超过 9")
        for index, ingredient in enumerate(ingredients):
            slots[index] = _ingredient_from_json(ingredient)

    return Recipe(path.stem, result_id, result_name, tuple(slots))


def compare_ingredients(
    guess: tuple[Ingredient, ...], answer: tuple[Ingredient, ...]
) -> tuple[GuessState, ...]:
    if len(guess) != len(answer):
        raise ValueError("猜测和答案的原料格数量必须一致")

    states = [GuessState.WRONG] * len(guess)
    remaining_guess: list[int] = []
    remaining_answer: list[int] = []
    for index, (guessed, expected) in enumerate(zip(guess, answer, strict=True)):
        if guessed.matches(expected):
            states[index] = GuessState.CORRECT
        else:
            remaining_guess.append(index)
            remaining_answer.append(index)

    # Maximum bipartite matching prevents broad tag ingredients from consuming
    # a slot required by a more specific repeated ingredient.
    matched_guess_by_answer: dict[int, int] = {}

    def assign(guess_index: int, visited: set[int]) -> bool:
        for answer_index in remaining_answer:
            if answer_index in visited:
                continue
            if not guess[guess_index].matches(answer[answer_index]):
                continue
            visited.add(answer_index)
            previous = matched_guess_by_answer.get(answer_index)
            if previous is None or assign(previous, visited):
                matched_guess_by_answer[answer_index] = guess_index
                return True
        return False

    remaining_guess.sort(key=lambda index: len(guess[index].options))
    for guess_index in remaining_guess:
        if assign(guess_index, set()):
            states[guess_index] = GuessState.PRESENT
    return tuple(states)


class RecipeBook:
    def __init__(
        self,
        recipes: list[Recipe],
        items_dir: Path,
        background_path: Path,
        item_names: dict[str, str] | None = None,
    ):
        if not recipes:
            raise MindleDataError("没有找到可用的工作台配方")
        self.recipes = recipes
        self.items_dir = items_dir
        self.background_path = background_path
        self.item_names = item_names or {}
        self.by_name: dict[str, Recipe] = {}
        for recipe in recipes:
            self.by_name.setdefault(recipe.result_name, recipe)

    @classmethod
    def load(
        cls,
        recipes_dir: Path,
        lang_file: Path,
        items_dir: Path,
        background_path: Path,
    ) -> Self:
        required = (recipes_dir, lang_file, items_dir, background_path)
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise MindleDataError("缺少猜配方资源：" + "、".join(missing))

        raw_lang = json.loads(lang_file.read_text(encoding="utf-8"))
        translations: dict[str, str] = {}
        for key, value in raw_lang.items():
            if key.startswith(("item.minecraft.", "block.minecraft.")):
                translations.setdefault(key.rsplit(".", 1)[-1], value)

        recipes: list[Recipe] = []
        errors: list[str] = []
        for recipe_path in sorted(recipes_dir.glob("*.json")):
            try:
                recipe = parse_recipe(recipe_path, translations)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{recipe_path.name}: {exc}")
                continue
            if recipe is not None:
                recipes.append(recipe)
        if not recipes and errors:
            raise MindleDataError("配方均无法解析；" + errors[0])
        return cls(recipes, items_dir, background_path, translations)

    def random_recipe(self) -> Recipe:
        return random.choice(self.recipes)

    def get(self, item_name: str) -> Recipe | None:
        return self.by_name.get(item_name.strip())

    def suggestions(self, query: str, limit: int = 10) -> list[str]:
        query = query.strip()
        if not query:
            return []
        containing = [name for name in self.by_name if query in name]
        if containing:
            containing.sort(key=lambda name: (len(name), name))
            return containing[:limit]
        ranked = sorted(
            self.by_name,
            key=lambda name: (
                -SequenceMatcher(None, query, name).ratio(),
                len(name),
                name,
            ),
        )
        return [name for name in ranked if SequenceMatcher(None, query, name).ratio() >= 0.35][
            :limit
        ]

    def texture_path(self, item_id: str) -> Path | None:
        direct = self.items_dir / f"{_strip_namespace(item_id)}.png"
        if direct.is_file():
            return direct
        states_dir = self.items_dir / _strip_namespace(item_id)
        if states_dir.is_dir():
            files = sorted(states_dir.rglob("*.png"), key=lambda path: path.name)
            if files:
                return files[0]
        return None

    def item_name(self, item_id: str) -> str:
        item_id = _strip_namespace(item_id)
        return self.item_names.get(item_id, item_id)


@lru_cache(maxsize=512)
def _load_texture(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA").copy()


class Mindle:
    SCALE = 4
    SLOT_ORIGIN = (30, 17)
    SLOT_STEP = 18
    SLOT_SIZE = 16
    OUTPUT_ORIGIN = (124, 35)
    OUTPUT_SIZE = SLOT_SIZE
    COLORS = {
        GuessState.CORRECT: (63, 198, 65, 255),
        GuessState.PRESENT: (211, 201, 52, 255),
        GuessState.WRONG: (201, 58, 61, 255),
    }

    def __init__(self, answer: Recipe, book: RecipeBook, max_attempts: int = 10):
        self.answer = answer
        self.book = book
        self.max_attempts = max_attempts
        self.guessed_item_ids: list[str] = []
        self.last_recipe: Recipe | None = None
        self.last_states: tuple[GuessState, ...] | None = None
        self.hinted_item_ids: set[str] = set()
        self.crystal_bonus: int = cfg.crystal_bonus

    @classmethod
    def random_mindle(cls, book: RecipeBook, max_attempts: int = 10) -> Self:
        return cls(book.random_recipe(), book, max_attempts)

    @property
    def result(self) -> str:
        return f"【物品】：{self.answer.result_name}"

    def guess(self, recipe: Recipe) -> GuessResult | None:
        if recipe.result_id in self.guessed_item_ids:
            return GuessResult.DUPLICATE
        self.guessed_item_ids.append(recipe.result_id)
        self.last_recipe = recipe
        if recipe.result_id == self.answer.result_id:
            # One item can have multiple recipes. A correct item guess should
            # still render every slot as correct when its recipe differs from
            # the randomly selected answer recipe.
            self.last_states = (GuessState.CORRECT,) * len(recipe.ingredients)
            return GuessResult.WIN
        self.last_states = compare_ingredients(recipe.ingredients, self.answer.ingredients)
        if len(self.guessed_item_ids) >= self.max_attempts:
            return GuessResult.LOSS
        return None

    def set_hinted_crystal_bonus(self):
        self.crystal_bonus = cfg.hinted_crystal_bonus

    def get_hint(self) -> RecipeHint | None:
        item_ids = list(
            dict.fromkeys(
                ingredient.options[0]
                for ingredient in self.answer.ingredients
                if ingredient.options
            )
        )
        if not item_ids:
            return None
        unrevealed = [item_id for item_id in item_ids if item_id not in self.hinted_item_ids]
        item_id = random.choice(unrevealed or item_ids)
        image_path = self.book.texture_path(item_id)
        if image_path is None:
            return None
        self.hinted_item_ids.add(item_id)
        return RecipeHint(
            item_id=f"minecraft:{_strip_namespace(item_id)}",
            item_name=self.book.item_name(item_id),
            image_path=image_path,
        )

    def _paste_item(self, canvas: Image.Image, item_id: str, box: tuple[int, int, int]):
        path = self.book.texture_path(item_id)
        if path is None:
            return
        x, y, size = box
        texture = _load_texture(path)
        texture.thumbnail((size, size), Image.Resampling.LANCZOS)
        px = x + (size - texture.width) // 2
        py = y + (size - texture.height) // 2
        canvas.alpha_composite(texture, (px, py))

    def draw(
        self,
        recipe: Recipe | None = None,
        states: tuple[GuessState, ...] | None = None,
    ) -> BytesIO:
        with Image.open(self.book.background_path) as background:
            canvas = background.convert("RGBA").resize(
                (background.width * self.SCALE, background.height * self.SCALE),
                Image.Resampling.NEAREST,
            )
        draw = ImageDraw.Draw(canvas)
        if states is None:
            states = self.last_states
        recipe = recipe or self.last_recipe
        if recipe is None:
            return save_png(canvas)

        for index, ingredient in enumerate(recipe.ingredients):
            row, col = divmod(index, 3)
            x = (self.SLOT_ORIGIN[0] + col * self.SLOT_STEP) * self.SCALE
            y = (self.SLOT_ORIGIN[1] + row * self.SLOT_STEP) * self.SCALE
            size = self.SLOT_SIZE * self.SCALE
            if states:
                draw.rectangle((x, y, x + size - 1, y + size - 1), fill=self.COLORS[states[index]])
            if not ingredient.is_air:
                self._paste_item(canvas, ingredient.options[0], (x, y, size))

        output_x = self.OUTPUT_ORIGIN[0] * self.SCALE
        output_y = self.OUTPUT_ORIGIN[1] * self.SCALE
        output_size = self.OUTPUT_SIZE * self.SCALE
        self._paste_item(canvas, recipe.result_id, (output_x, output_y, output_size))
        return save_png(canvas)
