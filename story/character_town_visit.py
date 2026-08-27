import json
import re
from functools import cache
from pathlib import Path

from story.story_parser import (
    get_story_scenes,
    parse_story_scene,
    _load_story_resources,
)
from story.story_types import StoryScene
from story.character_town_visit_wikitext import scene_to_wikitext
from utils.utils import db_root, load_db, load_text


@cache
def _find_visit_db_names() -> list[str]:
    return sorted(
        f.stem
        for f in db_root.iterdir()
        if f.name.startswith("story_visit_")
        and f.name.endswith("@story.json")
    )


@cache
def get_visit_scenes() -> dict[str, StoryScene]:
    resources = _load_story_resources()
    result: dict[str, StoryScene] = {}
    for db_name in _find_visit_db_names():
        raw = json.loads((db_root / f"{db_name}.json").read_text(encoding="utf-8"))
        for entry in raw:
            scene = parse_story_scene(entry, resources)
            result[scene.scene_id] = scene
    return result


@cache
def get_town_visit_metadata() -> list[dict]:
    return load_db("town_visit@town_story_visit")


@cache
def _load_visit_names() -> dict[str, str]:
    return load_text("town_normal_visit")

def get_visit_name(scene_id: str) -> str:
    names = _load_visit_names()
    parts = scene_id.split("_")
    if len(parts) == 3:
        char_id = parts[1]
        visit_num = int(parts[2])
        name_key = f"name@normal_visit_{char_id}_{visit_num}"
        return names.get(name_key, "")
    return ""


@cache
def _visit_scene_to_area() -> dict[str, str]:
    result = {}
    for entry in load_db("town_visit@town_normal_visit"):
        scene_id = entry.get("link_story_id", "")
        area_id = entry.get("link_town_area_id", "")
        if scene_id and area_id:
            result[scene_id] = area_id
    return result


@cache
def _load_area_names() -> dict[str, str]:
    return load_text("town_area")


def get_visit_area_name(scene_id: str) -> str:
    area_id = _visit_scene_to_area().get(scene_id, "")
    if not area_id:
        return ""
    return _load_area_names().get(f"name@{area_id}", "")


def _extract_char_id(visit_id: str) -> int | None:
    m = re.match(r"visit_(\d+)_\d+", visit_id)
    return int(m.group(1)) if m else None


def _natural_sort_key(s: str) -> list:
    parts = re.split(r"(\d+)", s)
    return [int(p) if p.isdigit() else p for p in parts]


@cache
def group_visits_by_character() -> dict[int, list[StoryScene]]:
    scenes = get_visit_scenes()
    by_char: dict[int, list[StoryScene]] = {}
    for scene in sorted(scenes.values(), key=lambda s: _natural_sort_key(s.scene_id)):
        char_id = _extract_char_id(scene.scene_id)
        if char_id is not None:
            by_char.setdefault(char_id, []).append(scene)
    return by_char


def visit_scene_to_wikitext(scene: StoryScene) -> str:
    return scene_to_wikitext(scene)


def character_town_visit_wikitext(char_id: int, scenes: list[StoryScene]) -> str:
    parts = []
    for scene in scenes:
        area_name = get_visit_area_name(scene.scene_id)
        visit_name = get_visit_name(scene.scene_id)
        wt = visit_scene_to_wikitext(scene)
        if wt:
            # header = f"{visit_name}\n\nArea: {area_name}"
            wt = f"{{{{StoryDialogueNarration|message='''{visit_name}'''}}}}\n{{{{StoryDialogueNarration|message=Area: {area_name}}}}}\n{wt}"
            parts.append(wt)
    if not parts:
        return ""
    header = "{{Combatant NavTab}}"
    return header + "\n\n{{StoryContainer|\n\n" + "\n{{StoryDialogueSeparator}}\n".join(parts) + "\n\n}}"


def save_character_town_visits():
    from utils.wiki_utils import save_wikitext_page
    from char_info.characters import parse_characters

    characters = parse_characters()
    grouped = group_visits_by_character()

    for char_id, scenes in sorted(grouped.items()):
        char = characters.get(char_id)
        if char is None:
            continue
        wt = character_town_visit_wikitext(char_id, scenes)
        if wt:
            save_wikitext_page(
                f"{char.name}/town visits",
                wt,
                summary="update town visit page",
            )


def main():
    save_character_town_visits()


if __name__ == "__main__":
    main()
