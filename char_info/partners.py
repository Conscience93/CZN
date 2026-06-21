import json
import re
from dataclasses import dataclass
from collections import defaultdict
from functools import cache

from pywikibot import Page
from pywikibot.pagegenerators import PreloadingGenerator

from utils.utils import load_text
from utils.utils import assets_root, db_root, load_db, resolve_text_markup


@dataclass
class Partner:
    id: int
    name: str


INFO_FIELDS = {
    "background_text",
    "specialty",
    "birth_day",
    "birth_month",
    "race_type",
    "specialty",
    "passive",
    "ego_skill",
    "cv_en",
    "cv_ja",
    "cv_ko",
    "cv_zhs",
}


def parse_bracket_list(s: str) -> list[str]:
    s = s.strip()
    if not s or s == "[]":
        return []
    inner = s.strip("[]")
    if not inner:
        return []
    return [item.strip() for item in inner.split(",") if item.strip()]


def maybe_int(value):
    try:
        return int(value)
    except TypeError:
        return value
    except ValueError:
        return value


def normalize_rarity(value: str) -> str:
    return value.removeprefix("RARITY_")


def rarity_sort_value(value: str) -> int:
    return {"SSR": 0, "SR": 1, "R": 2}.get(value, 99)


def clean_skill_text(text: str) -> str:
    text = re.sub(r"\$([^$]*?)(?:#\d+)?\$", r"\1", text)
    text = re.sub(r"</>", "", text)
    return re.sub(r"</?(?!br\b)[^>]+>", "", text, flags=re.IGNORECASE)


def resolve_placeholders(
    text: str, skill_eff_ids: list[str], eff_map: dict[str, dict]
) -> str:
    def get_eff_value(idx: int, key: str, fallback: str) -> str:
        if idx < len(skill_eff_ids):
            eff = eff_map.get(skill_eff_ids[idx])
            if eff:
                return eff.get(key, fallback)
        return fallback

    def replacer(match):
        ph = match.group(1)
        m = re.match(
            r"result_(?:coeff_)?(?:pct_off_)?ev_(\d+)$|"
            r"result_coeff_pct_off_ev_(\d+)$|"
            r"result_damage(?:_pct_off)?_(\d+)$",
            ph,
        )
        if m:
            idx = int(next(group for group in m.groups() if group is not None))
            return get_eff_value(idx, "eff_value", match.group(0))
        m = re.match(r"result_ecv_(\d+)$", ph)
        if m:
            return get_eff_value(int(m.group(1)), "eff_count_value", match.group(0))
        return match.group(0)

    return re.sub(r"#([a-zA-Z_]\w*)#", replacer, text)


def resolve_partner_ego_placeholders(text: str, skill_eff_ids: list[str]) -> str:
    card_skill_eff_map = load_partner_card_skill_eff_map()
    cs_map = load_partner_cs_map()
    cs_skill_eff_map = load_partner_cs_skill_eff_map()
    text = resolve_placeholders(text, skill_eff_ids, card_skill_eff_map)

    def get_cs_eff_value(
        skill_eff_idx: int, cs_eff_idx: int, key: str, fallback: str
    ) -> str:
        if skill_eff_idx >= len(skill_eff_ids):
            return fallback
        skill_eff = card_skill_eff_map.get(skill_eff_ids[skill_eff_idx])
        if not skill_eff:
            return fallback
        cs_ids = parse_bracket_list(skill_eff.get("link_cs_id", "[]"))
        for cs_id in cs_ids:
            cs = cs_map.get(cs_id)
            if not cs:
                continue
            cs_skill_eff_ids = parse_bracket_list(cs.get("link_skill_eff_id", "[]"))
            if cs_eff_idx >= len(cs_skill_eff_ids):
                continue
            cs_skill_eff = cs_skill_eff_map.get(cs_skill_eff_ids[cs_eff_idx])
            if cs_skill_eff:
                return cs_skill_eff.get(key, fallback)
        return fallback

    def replacer(match):
        ph = match.group(1)
        if ph == "match_comb_char":
            return "assigned combatant"
        m = re.match(r"cs_ev_(\d+)_(\d+)$", ph)
        if m:
            return get_cs_eff_value(
                int(m.group(1)), int(m.group(2)), "eff_value", match.group(0)
            )
        m = re.match(r"cs_ecv_(\d+)_(\d+)$", ph)
        if m:
            return get_cs_eff_value(
                int(m.group(1)), int(m.group(2)), "eff_count_value", match.group(0)
            )
        return match.group(0)

    return re.sub(r"#([a-zA-Z_]\w*)#", replacer, text)


def ego_desc_text(card: dict) -> str:
    desc_outgame = card.get("desc_outgame", "")
    if desc_outgame and desc_outgame != "none":
        return desc_outgame
    return card.get("desc", "")


@cache
def load_partner_base_map() -> dict[int, dict]:
    result = {}
    for entry in load_db("partner_base@char_partner"):
        try:
            partner_id = int(entry["id"])
        except KeyError:
            continue
        except TypeError:
            continue
        except ValueError:
            continue
        result[partner_id] = entry
    return result


@cache
def load_partner_char_base_map() -> dict[int, dict]:
    result = {}
    for entry in load_db("partner_base@char_base"):
        try:
            partner_id = int(entry["id"])
        except KeyError:
            continue
        except TypeError:
            continue
        except ValueError:
            continue
        result[partner_id] = entry
    return result


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
def load_released_partner_ids() -> set[int]:
    portrait_dir = assets_root / "face/character"
    return {
        partner_id
        for partner_id in load_partner_base_map()
        if (portrait_dir / f"portrait_character_{partner_id}.png").exists()
    }


@cache
def load_partner_card_map() -> dict[str, dict]:
    result = {}
    raw_entries = json.loads(
        (db_root / "card(partner)@card.json").read_text(encoding="utf-8")
    )
    raw_by_id = {entry.get("id", ""): entry for entry in raw_entries}
    for entry in load_db("card(partner)@card"):
        card_id = entry.get("id", "")
        if card_id and card_id != "none":
            entry["raw_card_category"] = raw_by_id.get(card_id, {}).get(
                "card_category", entry.get("card_category", "")
            )
            result[card_id] = entry
    return result


@cache
def load_partner_card_skill_eff_map() -> dict[str, dict]:
    result = {}
    for entry in load_db("card(partner)@skill_eff"):
        eff_id = entry.get("id", "")
        if eff_id and eff_id != "none":
            result[eff_id] = entry
    return result


@cache
def load_partner_cs_map() -> dict[str, dict]:
    result = {}
    for entry in load_db("cs(partner)@cs"):
        cs_id = entry.get("id", "")
        if cs_id and cs_id != "none":
            result[cs_id] = entry
    return result


@cache
def load_partner_cs_skill_eff_map() -> dict[str, dict]:
    result = {}
    for entry in load_db("cs(partner)@skill_eff"):
        eff_id = entry.get("id", "")
        if eff_id and eff_id != "none":
            result[eff_id] = entry
    return result


def _partner_passive_db_names(suffix: str) -> list[str]:
    return sorted(
        f.stem
        for f in db_root.iterdir()
        if f.name.startswith("partner_passive") and f.name.endswith(f"@{suffix}.json")
    )


@cache
def load_partner_passive_groups() -> dict[str, list[dict]]:
    result = defaultdict(dict)
    for db_name in _partner_passive_db_names("partner_passive"):
        for entry in load_db(db_name):
            group = entry.get("group", "")
            passive_id = entry.get("id", "")
            if group and passive_id and passive_id != "none":
                result[group][passive_id] = entry
    return {group: list(entries.values()) for group, entries in result.items()}


@cache
def load_partner_passive_cs_map() -> dict[str, dict]:
    result = {}
    for db_name in _partner_passive_db_names("cs"):
        for entry in load_db(db_name):
            cs_id = entry.get("id", "")
            if cs_id and cs_id != "none":
                result[cs_id] = entry
    return result


@cache
def load_partner_passive_skill_eff_map() -> dict[str, dict]:
    result = {}
    for db_name in _partner_passive_db_names("skill_eff"):
        for entry in load_db(db_name):
            eff_id = entry.get("id", "")
            if eff_id and eff_id != "none":
                result[eff_id] = entry
    return result


def parse_partner_ego_skill(base_entry: dict) -> dict | None:
    card_id = base_entry.get("link_card_id", "")
    card = load_partner_card_map().get(card_id)
    if not card:
        return None

    skill_eff_ids = parse_bracket_list(card.get("link_skill_eff_id", "[]"))
    desc = clean_skill_text(
        resolve_partner_ego_placeholders(ego_desc_text(card), skill_eff_ids)
    )

    return {
        "id": card["id"],
        "name": clean_skill_text(card.get("name", "")),
        "cost": maybe_int(card.get("cost", "")),
        "category": card.get("raw_card_category", card.get("card_category", "")),
        "desc": desc,
    }


def _passive_skill_eff_ids(passive_entry: dict) -> list[str]:
    result = parse_bracket_list(passive_entry.get("link_skill_eff_id", "[]"))
    cs_map = load_partner_passive_cs_map()
    for cs_id in parse_bracket_list(passive_entry.get("link_cs_id", "[]")):
        cs = cs_map.get(cs_id)
        if cs:
            result.extend(parse_bracket_list(cs.get("link_skill_eff_id", "[]")))
    return result


def _passive_sort_value(value):
    if isinstance(value, int):
        return (0, value)
    return (1, str(value))


def parse_partner_passive_skills(base_entry: dict) -> list[dict]:
    group = base_entry.get("link_partner_passive_group", "")
    passives = []
    for entry in load_partner_passive_groups().get(group, []):
        skill_eff_ids = _passive_skill_eff_ids(entry)
        desc = entry.get("outgame_description") or entry.get("description", "")
        desc = clean_skill_text(
            resolve_placeholders(
                desc, skill_eff_ids, load_partner_passive_skill_eff_map()
            )
        )
        passives.append(
            {
                "id": entry["id"],
                "class": maybe_int(entry.get("class", "")),
                "level": maybe_int(entry.get("level", "")),
                "name": clean_skill_text(entry.get("name", "")),
                "desc": desc,
            }
        )

    return sorted(
        passives,
        key=lambda passive: (
            _passive_sort_value(passive["class"]),
            _passive_sort_value(passive["level"]),
            passive["id"],
        ),
    )


@cache
def parse_partners() -> dict[int, Partner]:
    result = {}
    for key, name in load_text("char_base").items():
        prefix, _, id_str = key.partition("@")
        if prefix == "name":
            partner_id = int(id_str)
            result[partner_id] = Partner(id=partner_id, name=name)

    # partner_text = load_text("char_base")
    # for key, name in partner_text.items():
    #     prefix, _, id_str = key.partition("@")
    #     if prefix == "name":
    #         if id_str.isnumeric():
    #             partner_id = int(id_str).removesuffix("_info")
    #             desc = partner_text.get(f'background_text@{id_str}').removesuffix("_info")
    #             result[name] = Partner(id=partner_id, name=name, desc=desc)

    return result


@cache
def parse_partner_info() -> dict[int, dict]:
    partners = parse_partners()
    released_partner_ids = load_released_partner_ids()
    db_data = load_db("supporter_info@supporter_info")
    result = {}
    for entry in db_data:
        char_id = int(entry["id"].removesuffix("_info"))
        if char_id not in partners or char_id not in released_partner_ids:
            continue
        result[char_id] = {"id": char_id, "name": partners[char_id].name} | {
            k: resolve_text_markup(entry[k]) for k in INFO_FIELDS if k in entry
        }
        if "specialty" in result[char_id]:
            result[char_id]["gift"] = result[char_id]["specialty"]
        char_base_entry = load_partner_char_base_map().get(char_id)
        if char_base_entry:
            rarity = normalize_rarity(char_base_entry.get("rarity", ""))
            if rarity:
                result[char_id]["rarity"] = rarity
            faction_id = char_base_entry.get("link_faction_id", "")
            sub_faction_id = char_base_entry.get("link_sub_faction_id", "")
            faction = load_faction_name_map().get(faction_id, faction_id)
            sub_faction = load_sub_faction_name_map().get(sub_faction_id, "")
            if faction:
                result[char_id]["faction"] = faction
            if sub_faction:
                result[char_id]["sub_faction"] = sub_faction
                result[char_id]["affiliation"] = sub_faction
                if faction:
                    result[char_id]["affiliation_full"] = f"{sub_faction} of {faction}"
            elif faction:
                result[char_id]["affiliation"] = faction
        base_entry = load_partner_base_map().get(char_id)
        if base_entry:
            class_id = base_entry.get("passive_condition_link_base_class_define_id", "")
            class_name = load_base_class_name_map().get(class_id, "")
            if class_name:
                result[char_id]["class"] = class_name
            ego_skill = parse_partner_ego_skill(base_entry)
            if ego_skill:
                result[char_id]["ego_skill"] = ego_skill
            result[char_id]["passive_skills"] = parse_partner_passive_skills(base_entry)
    return result


def partner_pages(page_suffix: str = "") -> list[Page]:
    from utils.wiki_utils import s

    pages = [
        Page(s, f'{info["name"]}{page_suffix}')
        for info in parse_partner_info().values()
    ]
    return list(PreloadingGenerator(pages))


def partner_list_info() -> list[dict]:
    result = []
    for info in sorted(
        parse_partner_info().values(),
        key=lambda partner: (rarity_sort_value(partner.get("rarity", "")), partner["id"]),
    ):
        ego_skill = info.get("ego_skill") or {}
        result.append(
            {
                "id": info["id"],
                "name": info["name"],
                "rarity": info.get("rarity", ""),
                "class": info.get("class", ""),
                "race_type": info.get("race_type", ""),
                "specialty": info.get("specialty", ""),
                "gift": info.get("gift", ""),
                "affiliation": info.get("affiliation", ""),
                "faction": info.get("faction", ""),
                "sub_faction": info.get("sub_faction", ""),
                "ego_skill_name": ego_skill.get("name", ""),
            }
        )
    return result


def save_partner_info():
    from utils.wiki_utils import save_json_page

    info_data = parse_partner_info()
    obj = {info["name"]: info for info in info_data.values()}
    save_json_page("Module:PartnerInfo/data.json", obj, summary="update partner info")

    save_json_page(
        "Module:PartnerInfo/data2.json",
        partner_list_info(),
        summary="update partner list info",
    )


def main():
    save_partner_info()


if __name__ == "__main__":
    main()
