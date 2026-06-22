from dataclasses import dataclass, field
from functools import cache

from pywikibot import Page
from pywikibot.pagegenerators import PreloadingGenerator

from utils.utils import load_db, load_text, resolve_text_markup
from utils.wiki_utils import save_json_page, s

INFO_FIELDS = {
    "background_text",
    "specialty",
    "birth_day",
    "birth_month",
    "hospitalization_reason",
    "race_type",
    "custom_category",
    "custom_text",
    "cv_en",
    "cv_ja",
    "cv_ko",
    "cv_zhs",
}


@dataclass
class Character:
    id: int
    name: str
    english_name: str = ""
    rarity: str = ""
    gender: str = ""
    affiliation: str = ""
    class_: str = field(default="")
    attribute: str = ""
    faction: str = ""
    sub_faction: str = ""
    playable: bool = field(default=False)

    def __hash__(self) -> int:
        return hash(self.id)


def normalize_rarity(value: str) -> str:
    return value.removeprefix("RARITY_")


def rarity_sort_value(value: str) -> int:
    return {"SSR": 0, "SR": 1, "R": 2}.get(value, 99)


@cache
def load_base_class_name_map() -> dict[str, str]:
    return {
        entry["id"]: entry.get("class_name", "")
        for entry in load_db("base_class_define@base_class_define")
        if entry.get("id")
    }


@cache
def load_faction_name_map() -> dict[str, str]:
    return {
        entry["id"]: entry.get("name", "")
        for entry in load_db("faction@faction")
        if entry.get("id")
    }


@cache
def load_sub_faction_name_map() -> dict[str, str]:
    return {
        entry["id"]: entry.get("name", "")
        for entry in load_db("faction@sub_faction")
        if entry.get("id")
    }


@cache
def parse_characters() -> dict[int, Character]:
    result = {}
    for key, name in load_text("char_base").items():
        prefix, _, id_str = key.partition("@")
        if prefix == "name":
            char_id = int(id_str)
            result[char_id] = Character(id=char_id, name=name)

    text = load_text("char_base")
    for entry in load_db("char_base@char_base"):
        char_id = int(entry["id"])
        if char_id not in result:
            continue
        char = result[char_id]
        char.english_name = text.get(f"english_name@{char_id}", "")
        char.rarity = normalize_rarity(entry.get("rarity", ""))
        char.gender = entry.get("gender_type", "").removeprefix("GENDER_").title()
        char.affiliation = entry.get("link_faction_id", "")
        faction_id = entry.get("link_faction_id", "")
        sub_faction_id = entry.get("link_sub_faction_id", "")
        char.faction = load_faction_name_map().get(faction_id, faction_id)
        sub_faction = load_sub_faction_name_map().get(sub_faction_id, sub_faction_id)
        if sub_faction != "No Affiliation":
            char.sub_faction = sub_faction
        char.playable = entry.get("char_use_playable") == "YES"

    for entry in load_db("char_base@char_combatant"):
        char_id = int(entry["id"])
        if char_id not in result:
            continue
        char = result[char_id]
        class_id = entry.get("link_base_class_define_id", "")
        char.class_ = load_base_class_name_map().get(class_id, class_id.title())
        char.attribute = entry.get("link_ego_type_id", "").title()

    return result


def combatant_pages(page_suffix: str = "") -> list[Page]:
    pages = [
        Page(s, f"{char.name}{page_suffix}")
        for char in parse_characters().values()
        if char.playable
    ]
    return list(PreloadingGenerator(pages))


@cache
def parse_character_info() -> dict[int, dict]:
    characters = parse_characters()
    db_data = load_db("combatant_info@combatant_info")
    result = {}
    for entry in db_data:
        char_id = int(entry["id"].removesuffix("_info"))
        if char_id not in characters:
            continue
        char = characters[char_id]
        result[char_id] = {
            "id": char_id,
            "name": char.name,
            "rarity": char.rarity,
            "class": char.class_,
            "faction": char.faction,
        } | {
            k: resolve_text_markup(entry[k]) for k in INFO_FIELDS if k in entry
        }
        if char.sub_faction:
            result[char_id]["sub_faction"] = char.sub_faction
    return result


def character_list_info() -> list[dict]:
    result = []
    playable_characters = {
        char_id
        for char_id, char in parse_characters().items()
        if char.playable
    }
    for info in sorted(
        (
            info
            for char_id, info in parse_character_info().items()
            if char_id in playable_characters
        ),
        key=lambda character: (
            rarity_sort_value(character.get("rarity", "")),
            character["id"],
        ),
    ):
        row = {
            "id": info["id"],
            "name": info["name"],
            "rarity": info.get("rarity", ""),
            "class": info.get("class", ""),
            "race_type": info.get("race_type", ""),
            "specialty": info.get("specialty", ""),
            "faction": info.get("faction", ""),
        }
        if info.get("sub_faction"):
            row["sub_faction"] = info["sub_faction"]
        result.append(row)
    return result


def save_character_info():
    info_data = parse_character_info()
    obj = {info["name"]: info for info in info_data.values()}
    save_json_page(
        "Module:CharacterInfo/data.json", obj, summary="update character info"
    )
    save_json_page(
        "Module:CharacterInfo/data2.json",
        character_list_info(),
        summary="update character list info",
    )


def main():
    save_character_info()


if __name__ == "__main__":
    main()
