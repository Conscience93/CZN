"""Upload exported combatant voice lines to the wiki."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

from audio.audio_utils import (
    DEFAULT_OGG_ROOT,
    DEFAULT_EXPORT_ROOT,
    DEFAULT_VOICE_LINES_JSON,
    DEFAULT_PARTNER_OGG_ROOT,
    DEFAULT_PARTNER_VOICE_LINES_JSON,
    VOICE_OGG_BITRATE,
    VOICE_WIKI_AUDIO_DISTANCE_THRESHOLD,
    VOICE_UPLOAD_LANGS,
    compute_audio_distance,
    voice_line_suffix,
)
from audio.voice_lines import VoiceLine, VoiceLineExport, load_voice_lines_json
from utils.upload_utils import UploadRequest, _upload_file, process_uploads


DEFAULT_WIKI_AUDIO_CACHE_ROOT = DEFAULT_EXPORT_ROOT / "wiki_cache"


def wav_to_ogg(wav_path: Path, ogg_path: Path) -> Path:
    ogg_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(wav_path),
            "-c:a",
            "libopus",
            "-b:a",
            VOICE_OGG_BITRATE,
            "-y",
            str(ogg_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ogg_path


def voice_line_file_name(line: VoiceLine, lang: str) -> str:
    character_name = wiki_file_component(line.character_name)
    suffix = wiki_file_component(voice_line_suffix(line.line_key))
    return f"Vo_{character_name}_{suffix}_{lang}.ogg"


def wiki_file_component(value: str) -> str:
    result: list[str] = []
    for char in value.strip().replace(" ", "_"):
        if char.isascii() and (char.isalnum() or char in "_.-"):
            result.append(char)
        elif char == "\t":
            result.append("_")
        elif not char.isascii():
            result.append(f"u{ord(char):04x}")
        else:
            result.append("_")
    return "".join(result) or "unknown"


def voice_line_ogg_path(line: VoiceLine, lang: str, ogg_root: Path = DEFAULT_OGG_ROOT) -> Path:
    return ogg_root / voice_line_file_name(line, lang)


def filter_voice_lines(
    export: VoiceLineExport,
    combatant_ids: set[int] | None = None,
) -> list[VoiceLine]:
    lines = export.lines
    if combatant_ids is not None:
        lines = [line for line in lines if line.combatant_id in combatant_ids]
    lines = [line for line in lines if not is_incomplete_voice_line(line)]
    return sorted(lines, key=lambda line: (line.combatant_id, line.line_key))


def is_incomplete_voice_line(line: VoiceLine) -> bool:
    return not {"ja", "ko"} <= line.wav_path.keys()


def convert_voice_line_files(
    export: VoiceLineExport,
    combatant_ids: set[int] | None = None,
    overwrite: bool = False,
    ogg_root: Path = DEFAULT_OGG_ROOT,
) -> dict[tuple[str, str], Path]:
    result: dict[tuple[str, str], Path] = {}
    for line in filter_voice_lines(export, combatant_ids):
        for lang in VOICE_UPLOAD_LANGS:
            wav_name = line.wav_path.get(lang, "")
            if not wav_name:
                continue
            wav_path = Path(wav_name)
            if not wav_path.exists():
                continue
            ogg_path = voice_line_ogg_path(line, lang, ogg_root)
            if overwrite or not ogg_path.exists():
                wav_to_ogg(wav_path, ogg_path)
            result[(line.line_key, lang)] = ogg_path
    return result


def build_voice_line_uploads(
    export: VoiceLineExport,
    combatant_ids: set[int] | None = None,
    ogg_root: Path = DEFAULT_OGG_ROOT,
) -> list[UploadRequest]:
    requests: list[UploadRequest] = []
    seen_targets: set[str] = set()
    for line in filter_voice_lines(export, combatant_ids):
        for lang in VOICE_UPLOAD_LANGS:
            if lang not in line.wav_path:
                continue
            ogg_path = voice_line_ogg_path(line, lang, ogg_root)
            if not ogg_path.exists():
                continue
            target = voice_line_file_name(line, lang)
            if target in seen_targets:
                continue
            seen_targets.add(target)
            requests.append(
                UploadRequest(
                    source=ogg_path,
                    target=target,
                    text=(
                        "{{FairUse}}\n"
                        f"[[Category:{line.character_name} voice lines]]"
                    ),
                    summary="upload voice lines",
                )
            )
    return requests


def upload_voice_line_files(
    export: VoiceLineExport,
    combatant_ids: set[int] | None = None,
    force: bool = False,
    overwrite_ogg: bool = False,
    compare_wiki_audio: bool = False,
    ogg_root: Path = DEFAULT_OGG_ROOT,
) -> None:
    convert_voice_line_files(
        export,
        combatant_ids=combatant_ids,
        overwrite=overwrite_ogg,
        ogg_root=ogg_root,
    )
    requests = build_voice_line_uploads(
        export,
        combatant_ids=combatant_ids,
        ogg_root=ogg_root,
    )
    process_uploads(requests, force=force)
    if compare_wiki_audio:
        compare_wiki_voice_line_files(requests)


def publish_combatant_voice_lines(
    combatant_ids: set[int] | None = None,
    voice_lines_json: Path = DEFAULT_VOICE_LINES_JSON,
    force: bool = False,
    overwrite_ogg: bool = False,
    compare_wiki_audio: bool = False,
    ogg_root: Path = DEFAULT_OGG_ROOT,
) -> None:
    export = load_voice_lines_json(voice_lines_json)
    upload_voice_line_files(
        export,
        combatant_ids=combatant_ids,
        force=force,
        overwrite_ogg=overwrite_ogg,
        compare_wiki_audio=compare_wiki_audio,
        ogg_root=ogg_root,
    )
    create_voice_line_audio_pages(export, combatant_ids=combatant_ids)


def publish_partner_voice_lines(
    partner_ids: set[int] | None = None,
    voice_lines_json: Path = DEFAULT_PARTNER_VOICE_LINES_JSON,
    force: bool = False,
    overwrite_ogg: bool = False,
    compare_wiki_audio: bool = False,
    ogg_root: Path = DEFAULT_PARTNER_OGG_ROOT,
) -> None:
    export = load_voice_lines_json(voice_lines_json)
    upload_voice_line_files(
        export,
        combatant_ids=partner_ids,
        force=force,
        overwrite_ogg=overwrite_ogg,
        compare_wiki_audio=compare_wiki_audio,
        ogg_root=ogg_root,
    )
    update_partner_voice_sections(export, partner_ids=partner_ids)


def compare_wiki_voice_line_files(
    requests: list[UploadRequest],
    cache_root: Path = DEFAULT_WIKI_AUDIO_CACHE_ROOT,
    distance_threshold: float = VOICE_WIKI_AUDIO_DISTANCE_THRESHOLD,
) -> None:
    from pywikibot import FilePage
    from utils.wiki_utils import s

    cache_root.mkdir(parents=True, exist_ok=True)
    seen_targets: set[str] = set()
    for request in requests:
        local_file = upload_request_local_file(request.source)
        if local_file is None or not local_file.exists():
            continue
        target_title = upload_request_file_title(request.target)
        if target_title in seen_targets:
            continue
        seen_targets.add(target_title)

        file_page = FilePage(s, target_title)
        cache_file = cache_root / local_file.name
        try:
            if not cache_file.exists():
                file_page.download(filename=str(cache_file))
            distance = compute_audio_distance(cache_file, local_file)
            if distance > distance_threshold:
                print(f"INFO: {target_title} differs from local audio: {distance}")
                _upload_file(
                    file_page.text,
                    file_page,
                    request.summary,
                    file=local_file,
                    force=True,
                )
                cache_file.unlink(missing_ok=True)
                shutil.copy2(local_file, cache_file)
        except Exception as e:
            print(f"WARNING: failed to compare {target_title}: {e}")


def upload_request_local_file(source) -> Path | None:
    if isinstance(source, Path):
        return source
    if callable(source):
        return source()
    return None


def upload_request_file_title(target: str) -> str:
    return target if target.startswith("File:") else f"File:{target}"


def build_voice_page_text(lines: list[VoiceLine]) -> str:
    sections: dict[str, list[VoiceLine]] = defaultdict(list)
    for line in sorted(lines, key=lambda item: item.line_key):
        sections[voice_line_section(line)].append(line)

    result = ["{{VoiceTop}}", ""]
    for section_name in ("General", "Combat", "Perk", "Special lobby", "Other"):
        section_lines = sections.get(section_name, [])
        if not section_lines:
            continue
        result.append(f"=={section_name}==")
        for line in section_lines:
            result.append(voice_line_template(line))
        result.append("")
    result.append("{{VoiceBottom}}")
    return "\n".join(result)


def parse_existing_voice_line_data(text: str) -> dict[str, dict[str, str]]:
    """Extract {{AudioRow|...}} fields from existing wikitext, keyed by line key."""
    import wikitextparser as wtp

    result: dict[str, dict[str, str]] = {}
    for template in wtp.parse(text or "").templates:
        if template.name.strip().lower() != "audiorow":
            continue
        fields = {
            arg.name.strip(): unescape_template_value(arg.value)
            for arg in template.arguments
        }
        key = fields.get("key", "").strip()
        if key:
            result[key] = fields
    return result


def unescape_template_value(value: str) -> str:
    return value.replace("{{!}}", "|").strip()


def fill_missing_voice_line_text(line: VoiceLine, existing: dict[str, str]) -> None:
    """Fill locally-missing transcripts/translations from existing wiki content.

    Manual corrections on the wiki should not be clobbered by a locally missing
    (empty) transcript or translation; only gaps are backfilled.
    """
    if not existing:
        return
    if not line.transcript.get("ja") and existing.get("text_ja"):
        line.transcript["ja"] = existing["text_ja"]
    if not line.transcript.get("ko") and existing.get("text_ko"):
        line.transcript["ko"] = existing["text_ko"]
    if not line.translation.get("en") and existing.get("text_en"):
        line.translation["en"] = existing["text_en"]


def create_voice_line_audio_pages(
    export: VoiceLineExport,
    combatant_ids: set[int] | None = None,
) -> None:
    from char_info.characters import combatant_pages
    from utils.wiki_utils import save_wikitext_page

    lines_by_combatant: dict[int, list[VoiceLine]] = defaultdict(list)
    for line in filter_voice_lines(export, combatant_ids):
        lines_by_combatant[line.combatant_id].append(line)

    page_by_title = {
        page.title(with_ns=False): page
        for page in combatant_pages("/voice")
    }
    for combatant_id, lines in sorted(lines_by_combatant.items()):
        if not lines:
            continue
        page_title = f"{lines[0].character_name}/voice"
        page = page_by_title.get(page_title)
        if page is None:
            continue
        existing_data = parse_existing_voice_line_data(page.text or "")
        for line in lines:
            fill_missing_voice_line_text(line, existing_data.get(line.line_key, {}))
        save_wikitext_page(
            page,
            build_voice_page_text(lines),
            summary="update voice lines page",
        )


def build_partner_voice_section_text(lines: list[VoiceLine]) -> str:
    return "\n".join(
        voice_line_template(line)
        for line in sorted(lines, key=lambda item: item.line_key)
    )


def update_partner_voice_sections(
    export: VoiceLineExport,
    partner_ids: set[int] | None = None,
) -> None:
    import wikitextparser as wtp

    from char_info.partners import partner_pages
    from utils.wiki_utils import save_wikitext_page

    lines_by_partner: dict[int, list[VoiceLine]] = defaultdict(list)
    for line in filter_voice_lines(export, partner_ids):
        lines_by_partner[line.combatant_id].append(line)

    page_by_title = {
        page.title(with_ns=False): page
        for page in partner_pages()
    }
    for _, lines in sorted(lines_by_partner.items()):
        page = page_by_title.get(lines[0].character_name)
        if page is None:
            print(f"WARNING: {lines[0].character_name} page not found")
            continue

        parsed = wtp.parse(page.text or "")
        voice_section = next(
            (
                section
                for section in parsed.sections
                if section.level == 2
                and (section.title or "").strip().lower() == "voice"
            ),
            None,
        )
        if voice_section is None:
            print(f"WARNING: {page.title(with_ns=False)} has no Voice section")
            continue

        existing_data = parse_existing_voice_line_data(voice_section.contents or "")
        for line in lines:
            fill_missing_voice_line_text(line, existing_data.get(line.line_key, {}))

        section_text = build_partner_voice_section_text(lines)
        voice_section.contents = f"\n{section_text}\n\n" if section_text else "\n"
        save_wikitext_page(
            page,
            str(parsed),
            summary="update partner voice lines",
        )


def voice_line_template(line: VoiceLine) -> str:
    fields = {
        "key": line.line_key,
        "title": line.title.get("en", ""),
        "file_ja": voice_line_file_name(line, "ja") if "ja" in line.wav_path else "",
        "file_ko": voice_line_file_name(line, "ko") if "ko" in line.wav_path else "",
        "text_ja": line.transcript.get("ja", ""),
        "text_ko": line.transcript.get("ko", ""),
        "text_en": line.translation.get("en", ""),
    }
    args = "|".join(
        f"{key}={escape_template_value(value)}"
        for key, value in fields.items()
    )
    return "{{AudioRow|" + args + "}}"


def escape_template_value(value: str) -> str:
    return (
        str(value)
        .replace("|", "{{!}}")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "")
        .replace("<p>", " ")
    )


def voice_line_section(line: VoiceLine) -> str:
    suffix = voice_line_suffix(line.line_key).lower()
    if re.match(
        r"^(attack|battle|begin|break|buff|camp|clear|collapse|crisis|critical|"
        r"death|death_collapse|defense|dmg|enter|failure|fatal|fatal_end|hit|info_voice_turn|"
        r"idle|lose|over|panic|pair_ux|ready|safe|skill|sp|stage|stress|turn|u\d|ug|ux|warning)",
        suffix,
    ):
        return "Combat"
    if re.match(
        r"^(chatter|captain|first|detailed|gacha|growth|move|lobby|title|touch|worry|"
        r"info_voice|small_talk|talk|text_get_char|voice_manage_enter|manage_enter|"
        r"voice_team_join|team_join)",
        suffix,
    ):
        return "General"
    if re.match(
        r"^(story_moment)",
        suffix,
    ):
        return "Special lobby"
    return "Other"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upload voice lines to the wiki.")
    parser.add_argument(
        "--kind",
        choices=("combatants", "partners"),
        default=None,
        help="Only publish one type of voice lines. Defaults to both.",
    )
    parser.add_argument(
        "--combatant-ids",
        default="",
        help="Comma-separated combatant IDs to process.",
    )
    parser.add_argument(
        "--partner-ids",
        default="",
        help="Comma-separated partner IDs to process.",
    )
    parser.add_argument(
        "--compare-wiki-audio",
        action="store_true",
        help="Compare existing wiki audio with local OGGs and reupload stale files.",
    )
    return parser.parse_args()


def parse_character_ids(value: str) -> set[int] | None:
    return {
        int(id_.strip())
        for id_ in value.split(",")
        if id_.strip()
    } or None


def main() -> None:
    args = parse_args()
    if args.kind in (None, "combatants"):
        publish_combatant_voice_lines(
            combatant_ids=parse_character_ids(args.combatant_ids),
            compare_wiki_audio=args.compare_wiki_audio,
        )
    if args.kind in (None, "partners"):
        publish_partner_voice_lines(
            partner_ids=parse_character_ids(args.partner_ids),
            compare_wiki_audio=args.compare_wiki_audio,
        )


if __name__ == "__main__":
    main()
