import re
from dataclasses import dataclass
from functools import cache

from utils.utils import load_db, load_text


STAT_FIELDS = (
    ("hp", "HP", "s_hp"),
    ("attack", "Attack", "s_atk"),
    ("defense", "Defense", "s_def"),
)
STAT_ID_MAP = {
    "S_HP": "hp",
    "S_ATK": "attack",
    "S_DEF": "defense",
    "Health": "hp",
    "Attack": "attack",
    "Defense": "defense",
}
LEVELS = tuple(range(1, 61))
ASCENDS = tuple(range(0, 6))


@dataclass(frozen=True)
class CombatantStats:
    id: int
    name: str
    level_values: dict[str, list[int]]
    ascend_values: dict[str, list[int]]


def _to_int(value: object) -> int:
    return int(str(value))


def _stat_deltas(entry: dict) -> dict[str, int]:
    deltas = {stat_key: 0 for stat_key, _, _ in STAT_FIELDS}
    for index in range(1, 4):
        stat_id = entry.get(f"stat{index}_link_stat_list_id")
        stat_key = STAT_ID_MAP.get(str(stat_id))
        if stat_key is None:
            continue
        deltas[stat_key] += _to_int(entry.get(f"stat{index}_value", 0))
    return deltas


@cache
def _level_entries_by_group() -> dict[str, dict[int, dict]]:
    result: dict[str, dict[int, dict]] = {}
    for entry in load_db("char_base@combatant_level"):
        result.setdefault(entry["group"], {})[_to_int(entry["level"])] = entry
    return result


@cache
def _ascend_entries_by_group() -> dict[str, dict[int, dict]]:
    result: dict[str, dict[int, dict]] = {}
    for entry in load_db("char_base@combatant_ascend"):
        result.setdefault(entry["group"], {})[_to_int(entry["ascend"])] = entry
    return result


@cache
def load_playable_combatant_stats() -> dict[int, CombatantStats]:
    names = load_text("char_base")
    playable_ids = {
        _to_int(entry["id"])
        for entry in load_db("char_base@char_base")
        if entry.get("char_use_playable") == "YES"
    }

    result: dict[int, CombatantStats] = {}
    for entry in load_db("char_base@char_combatant"):
        char_id = _to_int(entry["id"])
        if char_id not in playable_ids:
            continue

        base_values = {
            stat_key: _to_int(entry[base_field])
            for stat_key, _, base_field in STAT_FIELDS
        }
        level_values = _cumulative_level_values(
            entry["link_combatant_level_group"], base_values
        )
        ascend_values = _cumulative_ascend_values(
            entry["link_combatant_ascend_group"]
        )

        result[char_id] = CombatantStats(
            id=char_id,
            name=names.get(f"name@{char_id}", str(char_id)),
            level_values=level_values,
            ascend_values=ascend_values,
        )
    return result


def _cumulative_level_values(
    group: str, base_values: dict[str, int]
) -> dict[str, list[int]]:
    group_entries = _level_entries_by_group().get(group, {})
    missing_levels = [level for level in LEVELS if level not in group_entries]
    if missing_levels:
        raise ValueError(f"Missing combatant level rows for {group}: {missing_levels}")

    current = dict(base_values)
    result = {stat_key: [] for stat_key, _, _ in STAT_FIELDS}
    for level in LEVELS:
        for stat_key, delta in _stat_deltas(group_entries[level]).items():
            current[stat_key] += delta
        for stat_key in result:
            result[stat_key].append(current[stat_key])
    return result


def _cumulative_ascend_values(group: str) -> dict[str, list[int]]:
    group_entries = _ascend_entries_by_group().get(group, {})
    missing_ascends = [ascend for ascend in ASCENDS if ascend not in group_entries]
    if missing_ascends:
        raise ValueError(
            f"Missing combatant ascend rows for {group}: {missing_ascends}"
        )

    current = {stat_key: 0 for stat_key, _, _ in STAT_FIELDS}
    result = {stat_key: [] for stat_key, _, _ in STAT_FIELDS}
    for ascend in ASCENDS:
        for stat_key, delta in _stat_deltas(group_entries[ascend]).items():
            current[stat_key] += delta
        for stat_key in result:
            result[stat_key].append(current[stat_key])
    return result


def _csv(values: list[int] | tuple[int, ...]) -> str:
    return ",".join(str(value) for value in values)


def render_stats_section(stats: CombatantStats) -> str:
    values = []
    for stat_key, label, _ in STAT_FIELDS:
        values.append(
            f"""{{{{StatCalc/value
| label = {label}
| name1 = level
| values1 = {_csv(stats.level_values[stat_key])}
| name2 = ascend
| values2 = {_csv(stats.ascend_values[stat_key])}
| formula = level + ascend
}}}}"""
        )

    values_text = "\n".join(values)
    return f"""== Stats ==
{{{{StatCalc
|1={{{{StatCalc/control
| name = level
| label = Level:
| levels = {_csv(LEVELS)}
}}}}
{{{{StatCalc/control
| name = ascend
| label = Ascend:
| levels = {_csv(ASCENDS)}
}}}}
<hr/>
<div class="stat-data-container">
{values_text}
</div>
}}}}"""


STATS_SECTION_RE = re.compile(
    r"^==\s*Stats\s*==\s*\n.*?(?=^==[^=\n].*?==\s*$|\Z)",
    re.MULTILINE | re.DOTALL,
)
EGO_HEADING_RE = re.compile(
    r"^==\s*Ego Manifestation\s*==\s*$",
    re.MULTILINE,
)


def replace_or_insert_stats_section(text: str, stats_section: str) -> str:
    normalized_section = stats_section.strip() + "\n\n"
    if STATS_SECTION_RE.search(text):
        return STATS_SECTION_RE.sub(lambda _: normalized_section, text, count=1)

    ego_match = EGO_HEADING_RE.search(text)
    if ego_match:
        return text[: ego_match.start()] + normalized_section + text[ego_match.start() :]

    suffix = "\n" if text.endswith("\n") or not text else "\n\n"
    return text + suffix + stats_section.strip() + "\n"


def update_combatant_stats_pages():
    from char_info.characters import combatant_pages
    from utils.wiki_utils import save_wikitext_page

    stats_by_name = {
        stats.name: stats
        for stats in load_playable_combatant_stats().values()
    }
    pages = {page.title(with_ns=False): page for page in combatant_pages()}

    for name, stats in stats_by_name.items():
        page = pages.get(name)
        if page is None or not page.exists():
            continue
        text = replace_or_insert_stats_section(
            page.text or "",
            render_stats_section(stats),
        )
        save_wikitext_page(page, text, summary="update combatant stats")
