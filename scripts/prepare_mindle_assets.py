import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from PIL import Image

SUPPORTED_TYPES = {"minecraft:crafting_shaped", "minecraft:crafting_shapeless"}


class TagResolver:
    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.cache: dict[str, list[str]] = {}

    def resolve(self, tag_id: str, stack: tuple[str, ...] = ()) -> list[str]:
        tag_id = tag_id.lstrip("#")
        if tag_id in self.cache:
            return self.cache[tag_id]
        if tag_id in stack:
            raise ValueError(f"标签循环引用：{' -> '.join((*stack, tag_id))}")
        namespace, name = tag_id.split(":", 1) if ":" in tag_id else ("minecraft", tag_id)
        path = self.data_root / namespace / "tags" / "item" / f"{name}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        values: list[str] = []
        for raw in data.get("values", []):
            value = raw.get("id") if isinstance(raw, dict) else raw
            if not isinstance(value, str):
                continue
            if value.startswith("#"):
                values.extend(self.resolve(value, (*stack, tag_id)))
            else:
                values.append(value)
        self.cache[tag_id] = list(dict.fromkeys(values))
        return self.cache[tag_id]


def expand_ingredient(value: Any, resolver: TagResolver) -> Any:
    if isinstance(value, str) and value.startswith("#"):
        return resolver.resolve(value)
    if isinstance(value, list):
        expanded: list[str] = []
        for item in value:
            result = expand_ingredient(item, resolver)
            expanded.extend(result if isinstance(result, list) else [result])
        return list(dict.fromkeys(expanded))
    if isinstance(value, dict):
        if "tag" in value:
            return resolver.resolve(value["tag"])
        if "item" in value:
            return value["item"]
    return value


def collect_item_ids(value: Any, output: set[str]):
    if isinstance(value, str):
        output.add(value)
    elif isinstance(value, list):
        for item in value:
            collect_item_ids(item, output)


def build_name_map(lang: dict) -> dict[str, str]:
    """从语言文件构建物品 ID（无命名空间）到中文名的映射，item 优先于 block，
    且优先使用带 .new 后缀的新版显示名（如旗帜图案、锻造模板）。"""
    names: dict[str, str] = {}
    for prefix in ("item.minecraft.", "block.minecraft."):
        for key, value in lang.items():
            if not isinstance(value, str) or not key.startswith(prefix):
                continue
            name = key[len(prefix) :]
            if name.endswith(".new"):
                names[name[:-4]] = value
            else:
                names.setdefault(name, value)
    return names


def prepare(source: Path, target: Path):
    recipes_source = source / "data" / "minecraft" / "recipe"
    rendered_source = source / "simple-image-renderer"
    lang_source = source / "assets" / "minecraft" / "lang" / "zh_cn.json"
    gui_source = (
        source / "assets" / "minecraft" / "textures" / "gui" / "container" / "crafting_table.png"
    )
    for path in (recipes_source, rendered_source, lang_source, gui_source):
        if not path.exists():
            raise FileNotFoundError(path)

    recipes_target = target / "recipe"
    items_target = target / "items"
    recipes_target.mkdir(parents=True, exist_ok=True)
    items_target.mkdir(parents=True, exist_ok=True)
    resolver = TagResolver(source / "data")
    item_ids: set[str] = set()
    recipe_count = 0

    for source_file in sorted(recipes_source.glob("*.json")):
        data = json.loads(source_file.read_text(encoding="utf-8"))
        if data.get("type") not in SUPPORTED_TYPES:
            continue
        if data["type"] == "minecraft:crafting_shaped":
            data["key"] = {
                symbol: expand_ingredient(value, resolver) for symbol, value in data["key"].items()
            }
            for value in data["key"].values():
                collect_item_ids(value, item_ids)
        else:
            data["ingredients"] = [
                expand_ingredient(value, resolver) for value in data["ingredients"]
            ]
            collect_item_ids(data["ingredients"], item_ids)
        collect_item_ids(data["result"]["id"], item_ids)
        (recipes_target / source_file.name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        recipe_count += 1

    lang = json.loads(lang_source.read_text(encoding="utf-8"))
    item_names = build_name_map(lang)
    missing: list[str] = []
    copied = 0
    for item_id in sorted(item_ids):
        name = item_names.get(item_id.split(":", 1)[-1])
        source_file = rendered_source / f"{name}.png" if name else None
        if source_file is None or not source_file.is_file():
            missing.append(item_id)
            continue
        shutil.copy2(source_file, items_target / f"{item_id.split(':', 1)[-1]}.png")
        copied += 1

    shutil.copy2(lang_source, target / "zh_cn.json")
    with Image.open(gui_source) as gui:
        gui.crop((0, 0, 176, 83)).save(target / "background.png")
    print(f"已准备 {recipe_count} 个配方、{copied} 张物品贴图")
    if missing:
        print(f"缺少 {len(missing)} 张贴图：{', '.join(missing[:20])}")


def main():
    parser = argparse.ArgumentParser(description="准备 mindle 所需的 Minecraft 资源")
    parser.add_argument("source", type=Path, nargs="?", default=Path("cache/26.2"))
    parser.add_argument("target", type=Path, nargs="?", default=Path("data/wordle/mindle"))
    args = parser.parse_args()
    prepare(args.source, args.target)


if __name__ == "__main__":
    main()
