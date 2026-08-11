import json
import tempfile
import unittest
from pathlib import Path

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from PIL import Image

nonebot.init(localstore_use_cwd=True)
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_plugin("nonebot_plugin_localstore")
nonebot.load_plugin("kanade_bot.plugins.command_counter")
nonebot.load_plugin("kanade_bot.plugins.wordle")

from kanade_bot.plugins.wordle.plugins.mindle.config import cfg
from kanade_bot.plugins.wordle.plugins.mindle.mindle import (
    AIR,
    GuessState,
    Ingredient,
    Mindle,
    Recipe,
    RecipeBook,
    compare_ingredients,
    parse_recipe,
)
from kanade_bot.plugins.wordle.util import GuessResult


class MindleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = RecipeBook.load(
            cfg.recipes_dir_path,
            cfg.lang_file_path,
            cfg.render_items_dir_path,
            cfg.background_image_path,
        )

    def test_config_paths(self):
        expected = Path.cwd() / "data" / "wordle" / "mindle"
        self.assertEqual(cfg.background_image_path, expected / "background.png")
        self.assertEqual(cfg.lang_file_path, expected / "zh_cn.json")
        self.assertEqual(cfg.render_items_dir_path, expected / "items")
        self.assertEqual(cfg.recipes_dir_path, expected / "recipe")

    def test_recipe_book_and_suggestions(self):
        self.assertGreater(len(self.book.recipes), 1000)
        self.assertIsNotNone(self.book.get("橡木告示牌"))
        suggestions = self.book.suggestions("按钮")
        self.assertEqual(len(suggestions), 10)
        self.assertTrue(all("按钮" in name for name in suggestions))

    def test_air_and_repeated_ingredient_comparison(self):
        wood = Ingredient(("oak_planks",))
        stone = Ingredient(("stone",))
        answer = (wood, wood, AIR, stone)
        guess = (wood, stone, AIR, wood)
        self.assertEqual(
            compare_ingredients(guess, answer),
            (
                GuessState.CORRECT,
                GuessState.PRESENT,
                GuessState.CORRECT,
                GuessState.PRESENT,
            ),
        )

    def test_moved_air_is_present(self):
        stone = Ingredient(("stone",))
        self.assertEqual(
            compare_ingredients((stone, AIR), (AIR, stone)),
            (GuessState.PRESENT, GuessState.PRESENT),
        )

    def test_shaped_recipe_is_centered(self):
        data = {
            "type": "minecraft:crafting_shaped",
            "key": {"#": "minecraft:stick"},
            "pattern": ["#", "#"],
            "result": {"id": "minecraft:test_item"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            recipe = parse_recipe(path, {"test_item": "测试物品"})
        self.assertIsNotNone(recipe)
        assert recipe is not None
        occupied = [
            index for index, item in enumerate(recipe.ingredients) if not item.is_air
        ]
        self.assertEqual(occupied, [1, 4])

    def test_guess_and_render(self):
        answer = self.book.get("橡木告示牌")
        wrong = self.book.get("白桦木告示牌")
        self.assertIsNotNone(answer)
        self.assertIsNotNone(wrong)
        assert answer is not None and wrong is not None
        game = Mindle(answer, self.book, max_attempts=2)
        self.assertIsNone(game.guess(wrong))
        with Image.open(game.draw()) as rendered:
            self.assertEqual(rendered.size, (704, 332))
            self.assertEqual(rendered.mode, "RGBA")
        self.assertEqual(Mindle.OUTPUT_SIZE, Mindle.SLOT_SIZE)

    def test_winning_alternate_recipe_renders_every_slot_correct(self):
        answer = Recipe(
            "answer_recipe",
            "shared_result",
            "相同产物",
            (Ingredient(("answer_item",)),) * 9,
        )
        guess = Recipe(
            "alternate_recipe",
            "shared_result",
            "相同产物",
            (Ingredient(("guess_item",)),) * 9,
        )
        game = Mindle(answer, self.book)

        self.assertEqual(game.guess(guess), GuessResult.WIN)
        self.assertEqual(game.last_states, (GuessState.CORRECT,) * 9)
        with Image.open(game.draw(game.answer)) as rendered:
            for index in range(9):
                row, col = divmod(index, 3)
                pixel = (
                    (Mindle.SLOT_ORIGIN[0] + col * Mindle.SLOT_STEP) * Mindle.SCALE,
                    (Mindle.SLOT_ORIGIN[1] + row * Mindle.SLOT_STEP) * Mindle.SCALE,
                )
                self.assertEqual(
                    rendered.getpixel(pixel), Mindle.COLORS[GuessState.CORRECT]
                )

    def test_hint_contains_name_id_and_image(self):
        answer = self.book.get("橡木告示牌")
        self.assertIsNotNone(answer)
        assert answer is not None
        game = Mindle(answer, self.book)
        recipe_hint = game.get_hint()
        self.assertIsNotNone(recipe_hint)
        assert recipe_hint is not None
        ingredient_ids = {
            ingredient.options[0]
            for ingredient in answer.ingredients
            if ingredient.options
        }
        self.assertIn(recipe_hint.item_id.removeprefix("minecraft:"), ingredient_ids)
        self.assertTrue(recipe_hint.item_name)
        self.assertTrue(recipe_hint.image_path.is_file())

        second_hint = game.get_hint()
        self.assertIsNotNone(second_hint)
        assert second_hint is not None
        self.assertNotEqual(recipe_hint.item_id, second_hint.item_id)
        self.assertEqual(
            {recipe_hint.item_id, second_hint.item_id},
            {f"minecraft:{item_id}" for item_id in ingredient_ids},
        )


if __name__ == "__main__":
    unittest.main()
