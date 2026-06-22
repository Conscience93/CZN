from __future__ import annotations

import json
import re
import shutil
from functools import cache
from pathlib import Path
from typing import Any

from utils.utils import assets_root, load_db, load_text, load_text_full


DEFAULT_EXPORT_ROOT = Path("exports/voice_lines/combatants")
DEFAULT_VOICE_LINES_JSON = DEFAULT_EXPORT_ROOT / "voice_lines.json"
DEFAULT_OGG_ROOT = DEFAULT_EXPORT_ROOT / "ogg"
DEFAULT_PARTNER_EXPORT_ROOT = Path("exports/voice_lines/partners")
DEFAULT_PARTNER_VOICE_LINES_JSON = DEFAULT_PARTNER_EXPORT_ROOT / "voice_lines.json"
DEFAULT_PARTNER_OGG_ROOT = DEFAULT_PARTNER_EXPORT_ROOT / "ogg"
DEFAULT_LANGS = ("ja", "ko")
VOICE_UPLOAD_LANGS = ("ja", "ko")
VOICE_OGG_BITRATE = "128k"
VOICE_WIKI_AUDIO_DISTANCE_THRESHOLD = 1
TRANSLATION_LANGS = ("en",)
TITLE_LANGS = ("en",)


def unique_wav_path(
    export_root: Path,
    combatant_id: int,
    character_name: str,
    lang: str,
    voice_event: str,
    stream_index: int,
    used_paths: set[Path],
) -> Path:
    character_dir = f"{combatant_id}_{sanitize_path_component(character_name)}"
    stem = sanitize_path_component(voice_event)
    path = export_root / character_dir / lang / f"{stem}.wav"
    if path not in used_paths:
        return path
    return export_root / character_dir / lang / f"{stem}_{stream_index}.wav"


def _extract_mfcc(path: Path, sr: int, n_mfcc: int):
    import librosa

    y, _ = librosa.load(path, sr=sr)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=n_mfcc)
    return (mfcc - mfcc.mean(axis=1, keepdims=True)) / (
        mfcc.std(axis=1, keepdims=True) + 1e-8
    )


def compute_audio_distance(
    p1: Path,
    p2: Path,
    sr: int = 22050,
    n_mfcc: int = 13,
) -> float:
    from fastdtw import fastdtw
    from scipy.spatial.distance import euclidean

    m1 = _extract_mfcc(p1, sr, n_mfcc).T
    m2 = _extract_mfcc(p2, sr, n_mfcc).T
    distance, path = fastdtw(m1, m2, dist=euclidean)
    return distance / len(path)


def voice_event_to_line_key(voice_event: str, combatant_id: int) -> str:
    prefix = f"vo_{combatant_id}_"
    if voice_event.startswith(prefix):
        return f"{combatant_id}_{voice_event.removeprefix(prefix)}"
    if voice_event.startswith("vo_"):
        return voice_event.removeprefix("vo_")
    return voice_event


def voice_line_text_id(line_key: str) -> str:
    return f"combatant_voice@voice_text@{line_key}"


def is_suffixed_lobby_enter_variant(line_key: str) -> bool:
    return re.match(r"^\d+_lobby_enter_01_(?:1|b)$", line_key) is not None


def voice_line_text_id_candidates(line_key: str) -> list[str]:
    exact_text_id = voice_line_text_id(line_key)
    match = re.match(r"^(\d+)_(.+)$", line_key)
    if not match:
        return [exact_text_id]

    combatant_id, suffix = match.groups()
    canonical_suffix = voice_line_title_alias(suffix) or suffix
    canonical_line_key = f"{combatant_id}_{canonical_suffix}"
    canonical_text_id = voice_line_text_id(canonical_line_key)
    char_info_text_id = (
        f"char_info_voice@voice_text@{combatant_id}_info_voice_{canonical_suffix}"
    )

    if canonical_suffix != suffix:
        candidates = [char_info_text_id, canonical_text_id, exact_text_id]
    else:
        candidates = [exact_text_id, char_info_text_id]

    return list(dict.fromkeys(candidates))


def resolve_voice_line_text(line_key: str, text_by_id: dict[str, str]) -> str:
    for text_id in voice_line_text_id_candidates(line_key):
        text = text_by_id.get(text_id, "")
        if text:
            return text
    return ""


def resolve_partner_voice_line_text(line_key: str, text_by_id: dict[str, str]) -> str:
    match = re.match(r"^(\d+)_(.+)$", line_key)
    if not match:
        candidates = [f"supporter_voice@voice_text@{line_key}"]
    else:
        partner_id, suffix = match.groups()
        voice_info = supporter_voice_info_by_id().get(line_key, {})
        candidates = [
            str(voice_info.get("voice_text_id", "")),
            f"supporter_voice@voice_text@{line_key}",
            f"char_info_voice@voice_text@{partner_id}_info_voice_{suffix}",
        ]

    for text_id in dict.fromkeys(candidate for candidate in candidates if candidate):
        text = text_by_id.get(text_id, "")
        if text:
            return text
    return ""


def voice_line_title(
    line_key: str,
    title_text_by_lang: dict[str, dict[str, str]],
) -> tuple[dict[str, str], str]:
    combatant_voice = combatant_voice_info_by_id().get(line_key)
    if combatant_voice is not None:
        title = str(combatant_voice.get("name", ""))
        if title and title != "none":
            return {"en": title}, "game_exact"

    english_titles = title_text_by_lang.get("en", {})
    title_key, source = voice_line_title_key(line_key, english_titles)
    if title_key:
        titles = {
            lang: text_by_id[title_key]
            for lang, text_by_id in title_text_by_lang.items()
            if title_key in text_by_id
        }
        if titles:
            return titles, source

    return {"en": guess_voice_line_title(voice_line_suffix(line_key))}, "guessed"


def partner_voice_line_title(
    line_key: str,
    title_text_by_lang: dict[str, dict[str, str]],
) -> tuple[dict[str, str], str]:
    supporter_voice = supporter_voice_info_by_id().get(line_key)
    if supporter_voice is not None:
        title = str(supporter_voice.get("name", ""))
        if title and title != "none" and not title.startswith("supporter_voice_"):
            return {"en": title}, "game_exact"

    return {"en": guess_voice_line_title(voice_line_suffix(line_key))}, "guessed"


def voice_line_title_key(line_key: str, english_titles: dict[str, str]) -> tuple[str, str]:
    suffix = voice_line_suffix(line_key)
    title_key = f"combatant_voice_{suffix}"
    if title_key in english_titles:
        return title_key, "game_generic"

    alias = voice_line_title_alias(suffix)
    if alias:
        title_key = f"combatant_voice_{alias}"
        if title_key in english_titles:
            return title_key, "alias"

    return "", ""


def voice_line_suffix(line_key: str) -> str:
    match = re.match(r"^\d+_(.+)$", line_key)
    return match.group(1) if match else line_key


def voice_line_title_alias(suffix: str) -> str:
    alias = {
        "battle_idle_01": "idle_01",
        "battle_idle_02": "idle_02",
        "cut_in_01": "skill_cutin_01",
        "dmg_01": "hit_01",
        "dmg_02": "hit_01",
        "failure_01": "stage_fail_01",
        "fatal_end_01": "death_collapse_01",
        "lobby_touch_01": "touch_01",
        "lobby_touch_02": "touch_02",
        "sp_01": "skill_special_01",
        "ug": "skill_ug_01",
        "ug_01": "skill_ug_01",
        "ug_b": "skill_ug_01",
        "ug2": "skill_ug_02",
        "ug_02": "skill_ug_02",
        "ux": "skill_ux_01",
        "ux1": "skill_ux_01",
        "ux_01": "skill_ux_01",
        "ux_1": "skill_ux_01",
        "ux_2": "skill_ux_01",
        "ux_3": "skill_ux_01",
        "ux_02": "card_08",
    }
    if suffix in alias:
        return alias[suffix]

    if suffix.startswith("ug "):
        return "skill_ug_01"

    match = re.match(r"^u([1-6])(?:_b|_2)?$", suffix)
    if match:
        number = int(match.group(1))
        if number <= 4:
            return f"skill_u{number}_01"
        if number == 5:
            return "skill_ug_01"
        return "skill_ug_02"

    match = re.match(r"^skill_u([1-4])_01(?:_b)?$", suffix)
    if match:
        return f"skill_u{match.group(1)}_01"

    match = re.match(r"^skill_(cutin|special)_01(?:_[abc]|_01)?$", suffix)
    if match:
        return f"skill_{match.group(1)}_01"

    match = re.match(r"^skill_02(?:-01|_b)$", suffix)
    if match:
        return "skill_02"

    match = re.match(r"^small_talk_(\d{2})_(?:0[12])$", suffix)
    if match:
        return f"small_talk_{match.group(1)}"

    match = re.match(r"^touch_(\d{2})_b$", suffix)
    if match:
        return f"touch_{match.group(1)}"

    match = re.match(r"^lobby_enter_01(?:_[1b])?$", suffix)
    if match:
        return "lobby_enter_01"

    match = re.match(r"^collapse_(0[1-3])_[bcd]$", suffix)
    if match:
        return f"collapse_{match.group(1)}"

    return ""


def guess_voice_line_title(suffix: str) -> str:
    cleaned = re.sub(r"\s+-\s+.*$", "", suffix)
    cleaned = re.sub(r"\s*\([^)]*\)", "", cleaned).strip("_ ")

    exact = {
        "add_01": "Additional Voice",
        "lose_01": "Defeat",
        "ready_01": "Ready",
        "sense_back_01": "Homescreen Return",
        "sense_birth_01": "Birthday Greetings",
        "sense_christmas_01": "Christmas Greetings",
        "sense_christmas_01_01": "Christmas Greetings",
        "sense_halloween_01": "Halloween Greetings",
        "sense_newyear_01": "New Year's Greetings",
        "sense_vallentine_01": "Valentine's Day Greetings",
        "sense_vallentine_01_b": "Valentine's Day Greetings",
        "talk_sp_01": "Special Chatter",
        "title": "Title Call",
        "title_ALT02": "Title Call 2",
        "worry_01": "Worry",
    }
    if cleaned in exact:
        return exact[cleaned]

    patterns = [
        (r"^attack_(\d+)$", "Attack {n}"),
        (r"^begin_(\d+)$", "Start {n}"),
        (r"^break_(\d+)$", "Break {n}"),
        (r"^cheer_(\d+)$", "Cheer {n}"),
        (r"^clear_(\d+)$", "Clear {n}"),
        (r"^collapse_attack_?(\d+)(?:_\d+)?$", "Mental Breakdown Attack {n}"),
        (r"^collapse_attack_voice_(\d+)$", "Mental Breakdown Attack {n}"),
        (r"^death_collapse_attack_(\d+)$", "Mental Breakdown Death Attack {n}"),
        (r"^collapse_idle_?(\d+)$", "Mental Breakdown Standby {n}"),
        (r"^collapse_(\d+)$", "Mental Breakdown {n}"),
        (r"^crisis_(\d+)$", "Crisis {n}"),
        (r"^critical_(\d+)$", "Critical {n}"),
        (r"^emotion(?:_voice)?_(\d+)$", "Emotion {n}"),
        (r"^enter_(\d+)(?:_(\d+))?$", "Entry {n}{suffix}"),
        (r"^friendship_moment_(\d+)(?:-(\d+))?$", "Friendship Moment {n}{suffix}"),
        (r"^grade_(\d+)$", "Grade Up {n}"),
        (r"^idle_(\d+)$", "Battle Standby {n}"),
        (r"^over_(\d+)$", "Over {n}"),
        (r"^panic1_voice_(\d+)$", "Mental Breakdown {n}"),
        (r"^pair_ux_(\d+)$", "Pair Ultimate {n}"),
        (r"^pv_(\d+)$", "Promotional Voice {n}"),
        (r"^sense_hello_(\d+)(?:[-_](\d+)|_b)?$", "Homescreen Greeting {n}{suffix}"),
        (r"^skill_(\d+)$", "Skill {n}"),
        (r"^skill_u(\d+)_01(?:_b)?$", "Skill {n}"),
        (r"^skill_ug_(\d+)$", "Skill {n}"),
        (r"^skill_ux_(\d+)$", "Skill {n}"),
        (r"^small_talk_(\d+)$", "Chatter {n}"),
        (r"^stage_enter_(\d+)$", "Mission Start {n}"),
        (r"^stage_success_(\d+)(?:_b)?$", "Mission Clear {n}"),
        (r"^story_moment_(\d+)(?:_b)?$", "Perk {n}"),
        (r"^title_(\d+|[abc])$", "Title Call {n}"),
        (r"^town_enter_default_(\d+)$", "Town Entry {n}"),
        (r"^town_policy_enter_(\d+)$", "Town Policy Entry {n}"),
        (r"^tutorial_(\d+)$", "Tutorial {n}"),
        (r"^u(\d+)(?:_b|_2)?$", "Skill {n}"),
        (r"^ug_?(\d+)?$", "Skill {n}"),
        (r"^ux(?:_?(\d+))?$", "Skill {n}"),
        (r"^warning_(\d+)$", "Warning {n}"),
    ]
    for pattern, template in patterns:
        match = re.match(pattern, cleaned)
        if match:
            number = match.group(1) or "1"
            suffix_number = match.group(2) if len(match.groups()) > 1 else ""
            suffix_text = f".{suffix_number}" if suffix_number else ""
            return template.format(
                n=int(number) if number.isdigit() else number.upper(),
                suffix=suffix_text,
            )

    return humanize_voice_line_suffix(cleaned)


def humanize_voice_line_suffix(suffix: str) -> str:
    words = re.sub(r"[_-]+", " ", suffix).strip()
    return words.title() if words else "Unknown Voice Line"


def sanitize_path_component(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value.strip(), flags=re.ASCII)
    value = value.strip("._")
    return value or "unknown"


def validate_vgmstream(vgmstream_cli: str) -> None:
    if shutil.which(vgmstream_cli) is None:
        raise RuntimeError(f"{vgmstream_cli!r} was not found in PATH")


@cache
def combatant_names() -> dict[int, str]:
    names = load_text("char_base")
    result: dict[int, str] = {}
    for entry in load_db("char_base@char_combatant"):
        combatant_id = int(entry["id"])
        result[combatant_id] = names.get(f"name@{combatant_id}", str(combatant_id))
    return result


@cache
def partner_names() -> dict[int, str]:
    from char_info.partners import parse_partners

    return {
        partner_id: partner.name
        for partner_id, partner in parse_partners().items()
    }


@cache
def combatant_voice_info_by_id() -> dict[str, dict[str, Any]]:
    return {str(entry["id"]): entry for entry in load_db("combatant_info@combatant_voice")}


@cache
def supporter_voice_info_by_id() -> dict[str, dict[str, Any]]:
    raw_path = assets_root / "db/supporter_info@supporter_voice.json"
    raw_entries = json.loads(raw_path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    text = load_text_full()
    for raw_entry in raw_entries:
        voice_id = str(raw_entry.get("id", ""))
        if not voice_id or voice_id == "none":
            continue
        result[voice_id] = {
            "name": text.get(str(raw_entry.get("name", "")), ""),
            "voice_text_id": str(raw_entry.get("voice_text", "")),
        }
    return result


@cache
def load_text_full_by_lang(lang: str) -> dict[str, str]:
    path = assets_root / "text" / lang / "text.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {entry["id"]: entry["text"] for entry in raw}
