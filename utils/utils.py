import json
import re
from functools import cache
from pathlib import Path

assets_root = Path("vendor/assets")
db_root = assets_root / "db"
game_install_root = Path("/mnt/ssd1/Games/Games/ChaosZeroNightmare/bin")
sound_root = game_install_root / "appdata/cznlive/data.unpacked/sound"


def resolve_text_markup(text: str) -> str:
    text = re.sub(
        r"<(color_\w+)>",
        lambda m: f'<span class="czn-color-{m.group(1)[6:].replace("_", "-")}">',
        text,
    )
    return text.replace("</>", "</span>")


@cache
def load_text_full() -> dict[str, str]:
    raw = json.loads(
        (assets_root / "text/en/text.json").read_text(encoding="utf-8")
    )
    return {entry["id"]: entry["text"] for entry in raw}


@cache
def _load_text_buckets() -> dict[str, dict[str, str]]:
    buckets: dict[str, dict[str, str]] = {}
    for id_, text in load_text_full().items():
        if "@" not in id_:
            continue
        prefix, _, key = id_.partition("@")
        buckets.setdefault(prefix, {})[key] = text
    return buckets


@cache
def load_text(name: str) -> dict[str, str]:
    return _load_text_buckets().get(name, {})


@cache
def _resolve_db_path(name: str) -> Path:
    path = db_root / f"{name}.json"
    if path.exists():
        return path

    match = re.fullmatch(r"(?P<prefix>.*\D)(?P<version>\d+)(?P<suffix>@.*)?", name)
    if match is None:
        return path

    suffix = match.group("suffix") or ""
    candidates = sorted(
        db_root.glob(f"{match.group('prefix')}[0-9]*{suffix}.json")
    )
    return candidates[-1] if candidates else path


@cache
def load_db(name: str) -> list[dict]:
    text = load_text_full()
    raw = json.loads(_resolve_db_path(name).read_text(encoding="utf-8"))
    result = []
    for entry in raw:
        resolved = {}
        for key, value in entry.items():
            if isinstance(value, str) and value in text:
                resolved[key] = text[value]
            elif isinstance(value, str) and value.lower() in text:
                resolved[key] = text[value.lower()]
            else:
                resolved[key] = value
        result.append(resolved)
    return result
