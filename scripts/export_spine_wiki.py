#!/usr/bin/env python3
"""Experimental Spine animation exporter for wiki format comparisons.

The script renders selected Spine 3.8 combatant assets in headless Chrome,
encodes deterministic WebM output from captured frames, and never overwrites
existing exports.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import http.server
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_ROOT = REPO_ROOT / "vendor" / "assets"
L2D_ROOT = REPO_ROOT / "vendor" / "l2d"
DB_CHAR_BASE = ASSETS_ROOT / "db" / "char_base@char_base.json"
TEXT_EN = ASSETS_ROOT / "text" / "en" / "text.json"
MODEL_DIR = ASSETS_ROOT / "model"
CARD_DIR = ASSETS_ROOT / "card"

DEFAULT_RUNTIME_URLS = [
    "https://unpkg.com/@esotericsoftware/spine-player@3.8.*/dist/iife/spine-player.js",
    "https://cdn.jsdelivr.net/gh/EsotericSoftware/spine-runtimes@3.8/spine-ts/build/spine-player.js",
]

MODEL_EXPORTS = [
    ("death", ("death_ready", "death")),
    ("idle", ("idle",)),
    ("move", ("move",)),
    ("victory", ("victory_ready", "victory")),
    ("collapse_idle", ("collapse_idle",)),
    ("enter", ("enter_play", "enter_end")),
]


@dataclasses.dataclass(frozen=True)
class Character:
    id: int
    name: str
    slug: str


@dataclasses.dataclass(frozen=True)
class LiveeventLayer:
    """A companion Spine skeleton layered with a liveevent character's main skeleton.

    Liveevent "cutin" scenes are split across several skeleton files sharing one
    coordinate space: bg_b (background, behind the character), bg_f (background,
    in front of the character), eff (effects, on top of everything), fade (a
    dim/transition overlay), and sometimes intro. All share the same root-bone
    origin, so no separate offset/layout data is needed to align them.
    """

    role: str
    index: int | None
    source_json: Path
    source_atlas: Path
    animation: str | None
    rendered: bool


@dataclasses.dataclass
class ExportJob:
    group: str
    character: Character
    source_json: Path
    source_atlas: Path
    output_dir: Path
    output_stem: str
    animations: tuple[str, ...]
    intrinsic_width: float
    intrinsic_height: float
    final_width: int
    final_height: int
    capture_width: int
    capture_height: int
    duration_seconds: float
    layers: tuple[LiveeventLayer, ...] = ()
    stage_frame: tuple[float, float, float, float] | None = None

    @property
    def key(self) -> str:
        return f"{self.group}/{self.character.id}/{self.output_stem}"


@dataclasses.dataclass(frozen=True)
class VideoCrop:
    x: int
    y: int
    width: int
    height: int
    source_width: int
    source_height: int


class ExportResultHandler(http.server.SimpleHTTPRequestHandler):
    """Serves repo files and receives deterministic export frames."""

    server: "ExportServer"

    def log_message(self, _format: str, *_args: Any) -> None:
        if self.server.verbose:
            super().log_message(_format, *_args)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/__spine_export__/next":
            try:
                job = self.server.jobs.get_nowait()
            except queue.Empty:
                # An empty queue can mean two different things: the whole export is
                # finished (all_dispatched), or we're between chunks waiting for the
                # current Chrome generation to be retired before the next chunk is
                # enqueued (see run_jobs). Only the former should stop the browser's
                # poll loop -- the latter must ask it to retry shortly, or the browser
                # would quit thinking the export is done while jobs remain.
                job = {"done": True} if self.server.all_dispatched else {"wait": True}
            self._send_json(job)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        job_id = params.get("id", [""])[0]
        length = int(self.headers.get("Content-Length", "0"))

        if parsed.path == "/__spine_export__/frame":
            frame_dir = self.server.frame_dirs.get(job_id)
            if frame_dir is None:
                self.send_error(404, "unknown export job")
                return
            try:
                frame_index = int(params.get("frame", [""])[0])
            except ValueError:
                self.send_error(400, "missing frame index")
                return
            if frame_index < 0:
                self.send_error(400, "invalid frame index")
                return
            target = frame_dir / f"frame_{frame_index:06d}.rgba"
            with target.open("wb") as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            self._send_json({"ok": True})
            return

        if parsed.path == "/__spine_export__/complete":
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError:
                payload = {"message": body}
            payload["id"] = job_id
            payload["ok"] = True
            payload["frames_complete"] = True
            self.server.results.put(payload)
            self._send_json({"ok": True})
            return

        if parsed.path == "/__spine_export__/error":
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"message": body}
            payload["id"] = job_id
            payload["ok"] = False
            self.server.results.put(payload)
            self._send_json({"ok": True})
            return

        self.send_error(404, "unknown export endpoint")

    def _send_json(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ExportServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Frames upload several at a time over short-lived HTTP/1.0 connections; the
    # default backlog of 5 overflows under that burst and the browser sees a
    # refused connection ("Failed to fetch").
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], handler: type[ExportResultHandler], directory: Path, verbose: bool) -> None:
        super().__init__(address, lambda *args, **kwargs: handler(*args, directory=str(directory), **kwargs))
        self.frame_dirs: dict[str, Path] = {}
        self.results: queue.Queue[dict[str, Any]] = queue.Queue()
        self.jobs: queue.Queue[dict[str, Any]] = queue.Queue()
        self.verbose = verbose
        self.all_dispatched = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Periodic Chrome restarts (--chrome-restart-interval) terminate the browser
        # while it may still hold a connection open (e.g. the long-poll GET for the
        # next job), which surfaces here as a routine connection reset/broken pipe.
        # That's expected, not a bug, so don't spam a traceback for it.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            if self.verbose:
                print(f"  (ignoring connection reset from {client_address}, likely a recycled Chrome process)", flush=True)
            return
        super().handle_error(request, client_address)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--character", action="append", help="Playable character ID or slug/name. May be passed more than once.")
    parser.add_argument("--type", default="all", help="Comma-separated export groups: all, battle_ready, model, card, liveevent.")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--output-scale", type=float, default=2.0, help="Scale intrinsic skeleton bounds to output dimensions.")
    parser.add_argument("--render-scale", choices=("1", "2", "3", "4"), default="1", help="Browser backing-canvas supersampling before ffmpeg downsampling. 1 disables supersampling (fastest).")
    parser.add_argument("--vp9-crf", type=int, default=18, help="VP9 CRF for deterministic and crop transcodes. Lower is higher quality.")
    parser.add_argument("--vp9-cpu-used", type=int, default=4, help="libvpx-vp9 speed setting. Higher is faster, usually larger/slightly lower quality.")
    parser.add_argument("--vp9-deadline", choices=("best", "good", "realtime"), default="good", help="libvpx-vp9 encoding deadline.")
    parser.add_argument("--ffmpeg-threads", type=int, default=0, help="ffmpeg encoder thread count. 0 lets ffmpeg choose.")
    parser.add_argument("--padding", type=float, default=24.0, help="Padding in skeleton coordinate units before output scaling.")
    parser.add_argument("--battle-crop-padding", type=int, default=16, help="Pixel padding to keep around detected battle_ready content after cropping.")
    parser.add_argument("--card-crop-padding", type=int, default=0, help="Pixel padding to keep around detected card content after cropping. Cards are transparent, so 0 keeps the art flush with no border.")
    parser.add_argument("--liveevent-crop-padding", type=int, default=32, help="Pixel padding to keep around detected liveevent content after cropping.")
    parser.add_argument("--battle-transparent", action="store_true", help="Keep battle_ready exports transparent. By default they use the preview's dark background to avoid additive-effect alpha artifacts.")
    parser.add_argument("--opaque-crop-threshold", type=int, default=8, help="RGB distance from the preview background required for opaque crop detection.")
    parser.add_argument("--no-battle-crop", action="store_true", help="Disable post-capture black border cropping for battle_ready exports.")
    parser.add_argument("--no-card-crop", action="store_true", help="Disable post-capture black border cropping for card exports.")
    parser.add_argument("--no-liveevent-crop", action="store_true", help="Disable post-capture transparent-margin cropping for liveevent exports.")
    parser.add_argument("--no-card-trim", action="store_true", help="Keep the zoom-in lead-in frames of card animations instead of starting playback at the settled card.")
    parser.add_argument("--max-edge", type=int, default=0, help="Optional maximum output width/height.")
    parser.add_argument("--card-max-edge", type=int, default=4096, help="Optional maximum card capture width/height before cropping.")
    parser.add_argument("--liveevent-max-edge", type=int, default=4096, help="Optional maximum liveevent capture width/height before cropping. Liveevent stage skeletons can be huge (e.g. panoramic scenes) and liveevent always renders at 4x, so an uncapped outlier can exceed the browser's array-buffer/texture limits.")
    parser.add_argument("--liveevent-render-roles", default="bg_b,bg_f,eff", help="Comma-separated companion layer roles to composite into the liveevent stable-pose capture (from bg_b, bg_f, eff, fade, intro).")
    parser.add_argument("--duration-pad", type=float, default=0.25)
    parser.add_argument("--capture-timeout", type=float, default=360.0)
    parser.add_argument("--force-swiftshader", action="store_true", help="Force Chrome's software WebGL renderer. Off by default so hardware GPU can be used.")
    parser.add_argument("--chrome-restart-interval", type=int, default=8, help="Restart the persistent Chrome after this many jobs to avoid WebGL context/GPU memory exhaustion during large batches. 0 disables periodic restarts.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--spine-runtime-url", action="append", help="Spine Player JS URL or local path. May be passed more than once.")
    parser.add_argument("--chrome", default=shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing exported WebM files instead of skipping them.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.liveevent_render_roles = frozenset(
        role.strip() for role in args.liveevent_render_roles.split(",") if role.strip()
    )
    groups = parse_groups(args.type)
    characters = filter_characters(load_playable_characters(), args.character)
    manifest: dict[str, Any] = {
        "settings": {
            "format": "webm",
            "groups": sorted(groups),
            "fps": args.fps,
            "output_scale": args.output_scale,
            "render_scale": render_scale(args),
            "vp9_crf": args.vp9_crf,
            "vp9_cpu_used": args.vp9_cpu_used,
            "vp9_deadline": args.vp9_deadline,
            "ffmpeg_threads": args.ffmpeg_threads,
            "padding": args.padding,
            "battle_transparent": args.battle_transparent,
            "opaque_crop_threshold": args.opaque_crop_threshold,
            "battle_crop": not args.no_battle_crop,
            "battle_crop_padding": args.battle_crop_padding,
            "card_crop": not args.no_card_crop,
            "card_crop_padding": args.card_crop_padding,
            "card_trim": not args.no_card_trim,
            "max_edge": args.max_edge,
            "card_max_edge": args.card_max_edge,
            "liveevent_max_edge": args.liveevent_max_edge,
            "liveevent_render_roles": sorted(args.liveevent_render_roles),
            "duration_pad": args.duration_pad,
            "force_swiftshader": args.force_swiftshader,
            "overwrite": args.overwrite,
        },
        "jobs": [],
        "skipped": [],
        "errors": [],
    }

    jobs, skipped = build_jobs(characters, groups, args)
    manifest["skipped"].extend(skipped)

    if args.dry_run:
        print_dry_run(jobs, skipped, args)
        return 0

    validate_tools(args, jobs)
    L2D_ROOT.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="spine-wiki-export-") as tmp:
        tmp_dir = Path(tmp)
        with run_export_server(args.verbose) as server:
            base_url = f"http://127.0.0.1:{server.server_port}"
            run_jobs(jobs, args, server, base_url, tmp_dir, manifest)

        if args.keep_temp:
            kept = L2D_ROOT / f"export_temp_{int(time.time())}"
            shutil.copytree(tmp_dir, kept, ignore=ignore_export_temp)
            manifest["kept_temp_dir"] = str(kept.relative_to(REPO_ROOT))

    write_manifest(manifest)
    print_summary(manifest)
    return 1 if manifest["errors"] else 0


EXPORT_GROUPS = {"battle_ready", "model", "card", "liveevent"}

# lobby is the idle/resting animation shared by the main character and its
# companion layers alike, and is more visually stable than the numbered step
# animations (which cycle through dialogue-specific poses/expressions).
LIVEEVENT_ANIMATION = "lobby"

# Companion layer filenames look like liveevent_{id}_bg_b_01.json, liveevent_{id}_fade.json, etc.
LIVEEVENT_LAYER_SUFFIX_RE = re.compile(r"^(bg_b|bg_f|eff|intro)_(\d+)$")
# fade/intro layers only carry intro/outro transition animations (no idle/lobby
# state), so they're assumed invisible during a settled mid-story pose and are
# excluded from the composite by default; --liveevent-render-roles can override this.
LIVEEVENT_DEFAULT_RENDER_ROLES = frozenset({"bg_b", "bg_f", "eff"})
LIVEEVENT_ROLE_DRAW_ORDER = {"bg_b": 0, "bg_f": 2, "eff": 3}  # main character implicitly sits at 1
# Within a role, higher-numbered layers sit farther from the camera and draw first:
# every liveevent_data/{id}.le sampled lists depth_infos front-to-back as
# [fade, bg_f_01, bg_f_02, ..., character, bg_b_01, bg_b_02, ...] (fade nearest,
# since it's a screen-covering transition; ascending bg_f/bg_b index moving away
# from the character in both directions). Sorting by descending index within each
# role reproduces that back-to-front draw order.


def parse_groups(raw: str) -> set[str]:
    groups = {part.strip().lower() for part in raw.split(",") if part.strip()}
    if not groups or "all" in groups:
        return set(EXPORT_GROUPS)
    invalid = groups - EXPORT_GROUPS
    if invalid:
        raise SystemExit(f"Unsupported type(s): {', '.join(sorted(invalid))}")
    return groups


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_text_map() -> dict[str, str]:
    entries = load_json(TEXT_EN)
    return {entry["id"]: entry["text"] for entry in entries}


def load_playable_characters() -> list[Character]:
    text = load_text_map()
    chars = []
    for entry in load_json(DB_CHAR_BASE):
        if entry.get("char_use_playable") != "YES":
            continue
        char_id = int(entry["id"])
        name = text.get(f"char_base@name@{char_id}", str(char_id))
        chars.append(Character(id=char_id, name=name, slug=slugify(name) or str(char_id)))
    return sorted(chars, key=lambda item: item.id)


def filter_characters(characters: list[Character], filters: list[str] | None) -> list[Character]:
    if not filters:
        return characters
    selected: list[Character] = []
    by_id = {str(char.id): char for char in characters}
    by_slug = {char.slug: char for char in characters}
    by_name = {char.name.lower(): char for char in characters}
    for raw in filters:
        key = raw.strip().lower()
        char = by_id.get(key) or by_slug.get(slugify(key)) or by_name.get(key)
        if char is None:
            raise SystemExit(f"Unknown playable character: {raw}")
        if char not in selected:
            selected.append(char)
    return selected


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return value


def classify_liveevent_layer(suffix: str) -> tuple[str, int | None]:
    if suffix == "fade":
        return "fade", None
    match = LIVEEVENT_LAYER_SUFFIX_RE.match(suffix)
    if match:
        return match.group(1), int(match.group(2))
    print(f"  warning: unrecognized liveevent layer suffix '{suffix}'; treating as unknown/unrendered", flush=True)
    return "unknown", None


def discover_liveevent_layers(char: Character) -> list[LiveeventLayer]:
    layers: list[LiveeventLayer] = []
    prefix = f"liveevent_{char.id}_"
    for source_json in sorted(MODEL_DIR.glob(f"{prefix}*.json")):
        suffix = source_json.stem[len(prefix):]
        source_atlas = source_json.with_suffix(".atlas")
        if not source_atlas.exists():
            print(f"  warning: liveevent layer {source_json.name} missing atlas; skipping", flush=True)
            continue
        role, index = classify_liveevent_layer(suffix)
        layers.append(
            LiveeventLayer(
                role=role,
                index=index,
                source_json=source_json,
                source_atlas=source_atlas,
                animation=None,
                rendered=False,
            )
        )
    return layers


def _bone_local_matrix(bone: dict[str, Any]) -> tuple[float, float, float, float]:
    """Return the bone's own (a, b, c, d) rotation/scale/shear matrix (no translation)."""
    rotation = float(bone.get("rotation") or 0.0)
    scale_x = float(bone["scaleX"]) if bone.get("scaleX") is not None else 1.0
    scale_y = float(bone["scaleY"]) if bone.get("scaleY") is not None else 1.0
    shear_x = float(bone.get("shearX") or 0.0)
    shear_y = float(bone.get("shearY") or 0.0)
    rad_x = math.radians(rotation + shear_x)
    rad_y = math.radians(rotation + shear_y + 90.0)
    a = math.cos(rad_x) * scale_x
    c = math.sin(rad_x) * scale_x
    b = math.cos(rad_y) * scale_y
    d = math.sin(rad_y) * scale_y
    return a, b, c, d


def bone_world_transforms(bones_data: list[dict[str, Any]]) -> dict[str, tuple[float, float, float, float, float, float]]:
    """Map bone name -> (a, b, c, d, worldX, worldY) affine transform, such that a local
    point (lx, ly) in that bone's space maps to world space as
    (a*lx + b*ly + worldX, c*lx + d*ly + worldY).

    Composes translation/rotation/scale/shear down the parent chain the same way
    Spine's runtime does for the default "normal" inherit mode -- the only mode
    observed on this project's liveevent stage skeletons.
    """
    by_name = {bone["name"]: bone for bone in bones_data}
    world: dict[str, tuple[float, float, float, float, float, float]] = {}

    def resolve(name: str) -> tuple[float, float, float, float, float, float]:
        if name in world:
            return world[name]
        bone = by_name[name]
        la, lb, lc, ld = _bone_local_matrix(bone)
        lx = float(bone.get("x") or 0.0)
        ly = float(bone.get("y") or 0.0)
        parent_name = bone.get("parent")
        if parent_name is None:
            result = (la, lb, lc, ld, lx, ly)
        else:
            pa, pb, pc, pd, px, py = resolve(parent_name)
            result = (
                pa * la + pb * lc,
                pa * lb + pb * ld,
                pc * la + pd * lc,
                pc * lb + pd * ld,
                pa * lx + pb * ly + px,
                pc * lx + pd * ly + py,
            )
        world[name] = result
        return result

    for bone in bones_data:
        resolve(bone["name"])
    return world


def stage_clip_bounds(stage_data: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Return (x, y, width, height) of the stage's clipping mask, in world-space.

    Every liveevent stage layer sampled (bg_b_01 across many characters) carries a
    clipping attachment -- usually named "1580X720_guide" -- whose vertices are
    reliably centered on the origin (e.g. x in [-790, 790], y in [-360, 360]) once
    converted to world space. The JSON's own top-level "skeleton" header
    (x/y/width/height) is not: it was observed to always declare x=0, y=0 regardless
    of character, which silently shifts the liveevent camera by roughly half the
    stage size and crops out whichever half of the background doesn't happen to be
    near the character. The clip polygon is the one source of truth for where the
    stage actually sits, so measure it directly instead of trusting the header.

    The clip's vertices are stored in the local space of whichever bone its slot is
    attached to, not world space. Most characters' clip bone is the root (or a
    zero-offset, unscaled camera bone), so raw vertices happen to equal world
    coordinates -- but at least one character (Diana, 1061) parents its clip under a
    bone with scaleX/scaleY = 0.28, which without this conversion inflates the
    computed stage frame ~3.6x and makes the final export render at a much lower
    resolution after the max-edge clamp kicks in. Applying the bone's full world
    transform fixes that case and is a no-op for the common case.
    """
    bones_data = stage_data.get("bones") or []
    transforms = bone_world_transforms(bones_data)
    slot_bones = {slot["name"]: slot.get("bone") for slot in stage_data.get("slots") or []}

    skins = stage_data.get("skins")
    # Spine 3.8's skin JSON has two shapes across versions: a list of
    # {"name", "attachments"} entries (what every asset here actually uses) or a bare
    # {skin_name: {slot: {attachment: {...}}}} dict (older format, kept as a fallback).
    if isinstance(skins, dict):
        attachments_list = list(skins.values())
    else:
        attachments_list = [entry.get("attachments", {}) for entry in skins or []]
    for attachments in attachments_list:
        for slot_name, slot_attachments in attachments.items():
            for attachment in slot_attachments.values():
                if attachment.get("type") != "clipping":
                    continue
                vertices = attachment.get("vertices") or []
                xs_local = vertices[0::2]
                ys_local = vertices[1::2]
                if not xs_local or not ys_local:
                    continue
                matrix = transforms.get(slot_bones.get(slot_name))
                if matrix is not None:
                    a, b, c, d, tx, ty = matrix
                    xs = [a * lx + b * ly + tx for lx, ly in zip(xs_local, ys_local)]
                    ys = [c * lx + d * ly + ty for lx, ly in zip(xs_local, ys_local)]
                else:
                    xs, ys = xs_local, ys_local
                return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
    return None


def canonical_stage_layer(layers: list[LiveeventLayer]) -> LiveeventLayer | None:
    def best(role: str) -> LiveeventLayer | None:
        candidates = sorted(
            (layer for layer in layers if layer.role == role),
            key=lambda layer: layer.index if layer.index is not None else 0,
        )
        return candidates[0] if candidates else None

    return best("bg_b") or best("bg_f")


def choose_layer_animation(available: list[str]) -> str | None:
    if "lobby" in available:
        return "lobby"
    if "animation1" in available:
        return "animation1"
    return available[0] if available else None


def resolve_liveevent_layer_animations(
    layers: list[LiveeventLayer], render_roles: frozenset[str]
) -> list[LiveeventLayer]:
    resolved: list[LiveeventLayer] = []
    for layer in layers:
        data = load_json(layer.source_json)
        available = list(data.get("animations", {}))
        resolved.append(
            dataclasses.replace(
                layer,
                animation=choose_layer_animation(available),
                rendered=layer.role in render_roles,
            )
        )
    resolved.sort(key=lambda layer: (LIVEEVENT_ROLE_DRAW_ORDER.get(layer.role, 99), -(layer.index if layer.index is not None else 0)))
    return resolved


def build_jobs(characters: list[Character], groups: set[str], args: argparse.Namespace) -> tuple[list[ExportJob], list[dict[str, Any]]]:
    jobs: list[ExportJob] = []
    skipped: list[dict[str, Any]] = []
    for char in characters:
        if "battle_ready" in groups:
            add_job(
                jobs,
                skipped,
                char,
                "battle_ready",
                MODEL_DIR / f"{char.id}_battle_ready.json",
                "b_idle",
                ("b_idle",),
                args,
            )
        if "model" in groups:
            for stem, animations in MODEL_EXPORTS:
                add_job(
                    jobs,
                    skipped,
                    char,
                    "model",
                    MODEL_DIR / f"{char.id}.json",
                    stem,
                    animations,
                    args,
                )
        if "liveevent" in groups:
            layers = resolve_liveevent_layer_animations(
                discover_liveevent_layers(char), args.liveevent_render_roles
            )
            stage_layer = canonical_stage_layer(layers)
            frame_override = None
            if stage_layer is not None:
                stage_data = load_json(stage_layer.source_json)
                frame_override = stage_clip_bounds(stage_data)
                if frame_override is None:
                    stage_skeleton = stage_data.get("skeleton", {})
                    stage_width = float(stage_skeleton.get("width") or 0)
                    stage_height = float(stage_skeleton.get("height") or 0)
                    if stage_width > 0 and stage_height > 0:
                        frame_override = (
                            float(stage_skeleton.get("x") or 0),
                            float(stage_skeleton.get("y") or 0),
                            stage_width,
                            stage_height,
                        )
            add_job(
                jobs,
                skipped,
                char,
                "liveevent",
                MODEL_DIR / f"liveevent_{char.id}.json",
                "liveevent",
                (LIVEEVENT_ANIMATION,),
                args,
                layers=tuple(layers),
                frame_override=frame_override,
            )
        if "card" in groups:
            card_jsons = sorted(CARD_DIR.glob(f"unique_{char.id}_*.json"))
            for source_json in card_jsons:
                number = int(source_json.stem.rsplit("_", 1)[1])
                source_atlas = source_json.with_suffix(".atlas")
                if not source_atlas.exists():
                    skipped.append(skip_payload(char, "card", source_json, "missing source atlas"))
                    continue
                data = load_json(source_json)
                animations = list(data.get("animations", {}))
                if not animations:
                    skipped.append(skip_payload(char, "card", source_json, "no animations"))
                    continue
                primary = "animation" if "animation" in animations else animations[0]
                ordered = [primary] + [name for name in animations if name != primary]
                for index, animation in enumerate(ordered):
                    stem = f"unique_{number:02d}" if index == 0 else f"unique_{number:02d}-{safe_stem(animation)}"
                    add_job(jobs, skipped, char, "card", source_json, stem, (animation,), args, data=data)
    return jobs, skipped


def add_job(
    jobs: list[ExportJob],
    skipped: list[dict[str, Any]],
    char: Character,
    group: str,
    source_json: Path,
    output_stem: str,
    animations: tuple[str, ...],
    args: argparse.Namespace,
    data: dict[str, Any] | None = None,
    layers: tuple[LiveeventLayer, ...] = (),
    frame_override: tuple[float, float, float, float] | None = None,
) -> None:
    source_atlas = source_json.with_suffix(".atlas")
    if not source_json.exists() or not source_atlas.exists():
        skipped.append(skip_payload(char, group, source_json, "missing source json or atlas"))
        return
    data = data or load_json(source_json)
    available = set(data.get("animations", {}))
    missing = [animation for animation in animations if animation not in available]
    if missing:
        skipped.append(skip_payload(char, group, source_json, "missing animation(s): " + ", ".join(missing)))
        return
    skeleton = data.get("skeleton", {})
    intrinsic_width = float(skeleton.get("width") or 0)
    intrinsic_height = float(skeleton.get("height") or 0)
    if intrinsic_width <= 0 or intrinsic_height <= 0:
        skipped.append(skip_payload(char, group, source_json, "missing intrinsic skeleton width/height"))
        return
    # Liveevent framing normally comes from the shared bg_b/bg_f "stage" rect
    # (frame_override), not the main skeleton's own declared bounds -- the latter
    # has been observed both far too small and wildly too large relative to the
    # actual composited scene.
    if frame_override is not None:
        _, _, frame_width, frame_height = frame_override
    else:
        frame_width, frame_height = intrinsic_width, intrinsic_height
    output_scale = group_output_scale(group, args)
    if group == "card":
        max_edge = args.card_max_edge
    elif group == "liveevent":
        max_edge = args.liveevent_max_edge
    else:
        max_edge = args.max_edge
    final_width, final_height = compute_dimensions(frame_width, frame_height, output_scale, args.padding, max_edge)
    capture_width = final_width
    capture_height = final_height
    duration = sum(animation_duration(data["animations"][animation]) for animation in animations)
    duration = duration + args.duration_pad
    jobs.append(
        ExportJob(
            group=group,
            character=char,
            source_json=source_json,
            source_atlas=source_atlas,
            output_dir=L2D_ROOT / group,
            output_stem=safe_stem(f"{char.slug}-{output_stem}"),
            animations=animations,
            intrinsic_width=intrinsic_width,
            intrinsic_height=intrinsic_height,
            final_width=final_width,
            final_height=final_height,
            capture_width=capture_width,
            capture_height=capture_height,
            duration_seconds=duration,
            layers=layers,
            stage_frame=frame_override,
        )
    )


def skip_payload(char: Character, group: str, source_json: Path, reason: str) -> dict[str, Any]:
    return {
        "character_id": char.id,
        "character": char.name,
        "group": group,
        "source": rel(source_json),
        "reason": reason,
    }


def compute_dimensions(width: float, height: float, output_scale: float, padding: float, max_edge: int) -> tuple[int, int]:
    padded_width = max(1.0, width + padding * 2.0)
    padded_height = max(1.0, height + padding * 2.0)
    final_width = padded_width * output_scale
    final_height = padded_height * output_scale
    if max_edge and max(final_width, final_height) > max_edge:
        scale = max_edge / max(final_width, final_height)
        final_width *= scale
        final_height *= scale
    return even_int(final_width), even_int(final_height)


def group_output_scale(group: str, args: argparse.Namespace) -> float:
    return args.output_scale


def even_int(value: float) -> int:
    number = max(2, int(math.ceil(value)))
    return number if number % 2 == 0 else number + 1


def safe_stem(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9_.+-]+", "-", value).strip("-")
    return value or "animation"


def animation_duration(animation: Any) -> float:
    return max(iter_times(animation), default=1.0)


def iter_times(value: Any) -> list[float]:
    times: list[float] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "time" and isinstance(item, (int, float)):
                times.append(float(item))
            else:
                times.extend(iter_times(item))
    elif isinstance(value, list):
        for item in value:
            times.extend(iter_times(item))
    return times


def print_dry_run(jobs: list[ExportJob], skipped: list[dict[str, Any]], args: argparse.Namespace) -> None:
    print(f"Planned jobs: {len(jobs)}")
    print("Format: webm, liveevent: png")
    for job in jobs:
        scale = job_render_scale(job, args)
        capture_width, capture_height = actual_capture_dimensions(job, scale)
        layer_summary = ""
        if job.group == "liveevent" and job.layers:
            rendered = sum(1 for layer in job.layers if layer.rendered)
            layer_summary = f" layers {rendered}/{len(job.layers)}"
        print(
            f"{job.key}: {rel(job.source_json)} "
            f"{'+'.join(job.animations)} "
            f"{job.final_width}x{job.final_height} "
            f"capture {capture_width}x{capture_height} "
            f"scale {scale}x "
            f"fps {args.fps}"
            f"{layer_summary}"
        )
    if skipped:
        print(f"\nSkipped before export: {len(skipped)}")
        for item in skipped[:40]:
            print(f"- {item['group']} {item['character_id']} {item['source']}: {item['reason']}")
        if len(skipped) > 40:
            print(f"- ... {len(skipped) - 40} more")


def validate_tools(args: argparse.Namespace, jobs: list[ExportJob]) -> None:
    commands = [("Chrome", args.chrome)]
    if jobs:
        commands.append(("ffmpeg", "ffmpeg"))
        commands.append(("ffprobe", "ffprobe"))
    for label, command in commands:
        if not shutil.which(command) and not Path(command).exists():
            raise SystemExit(f"{label} executable not found: {command}")


@contextlib.contextmanager
def run_export_server(verbose: bool) -> Any:
    server = ExportServer(("127.0.0.1", find_free_port()), ExportResultHandler, REPO_ROOT, verbose)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ignore_export_temp(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name.startswith("chrome-")}


def run_jobs(
    jobs: list[ExportJob],
    args: argparse.Namespace,
    server: ExportServer,
    base_url: str,
    tmp_dir: Path,
    manifest: dict[str, Any],
) -> None:
    """Render every job in a long-lived Chrome that pulls work from a queue.

    Chrome cold-starts a process, initialises the GPU, and re-fetches the Spine
    runtime from the CDN on every launch, so launching it once per job dominated
    the export wall time. Here Chrome loads the controller page once and pulls each
    job from /__spine_export__/next, keeping the runtime and GL context warm; Python
    encodes each job's frames as the browser moves on to the next, overlapping the
    two stages. The browser's next-job fetch runs as soon as it POSTs /complete for
    the previous one -- entirely independent of Python's main loop -- so jobs are
    only ever enqueued in chunks of --chrome-restart-interval: enqueueing the next
    chunk (and launching its Chrome) only happens after the current chunk's Chrome
    has been fully retired, so the browser can never dequeue a job that's about to
    be lost to a restart. (An earlier version enqueued every job upfront and
    restarted based on a completed-job counter; the browser would sometimes already
    have dequeued the next job by the time Python decided to restart, silently
    dropping that job and hanging forever waiting for a result that would never
    arrive.) The Chrome process is also recycled immediately on a WebGL context
    error, since a single GL context slowly exhausts GPU resources over a large
    batch.
    """
    plans: list[tuple[ExportJob, str, Path, Path]] = []
    for index, job in enumerate(jobs, start=1):
        target = job.output_dir / f"{job.output_stem}{output_extension(job)}"
        if target.exists() and not args.overwrite:
            manifest["skipped"].append({
                "skipped": True,
                "key": job.key,
                "reason": "target already exists",
                "existing": [rel(target)],
            })
            continue
        job.output_dir.mkdir(parents=True, exist_ok=True)
        capture_id = f"{int(time.time() * 1000)}-{os.getpid()}-{index}-{safe_stem(job.key)}"
        frame_dir = tmp_dir / f"{capture_id}-frames"
        frame_dir.mkdir(parents=True)
        server.frame_dirs[capture_id] = frame_dir
        plans.append((job, capture_id, frame_dir, target))

    if not plans:
        return

    url = build_controller_url(base_url, runtime_urls(args))
    restart_interval = max(0, int(args.chrome_restart_interval))
    chunk_size = restart_interval if restart_interval else len(plans)
    total = len(plans)
    chrome: subprocess.Popen[bytes] | None = None
    try:
        chunk_start = 0
        while chunk_start < total:
            chunk_end = min(chunk_start + chunk_size, total)
            chunk = plans[chunk_start:chunk_end]
            server.all_dispatched = chunk_end >= total
            for job, capture_id, frame_dir, target in chunk:
                server.jobs.put(job_descriptor(capture_id, job, args, job_render_scale(job, args)))

            chrome = launch_chrome(args, url, tmp_dir / f"chrome-controller-{chunk_start}")
            for offset, (job, capture_id, frame_dir, target) in enumerate(chunk):
                index = chunk_start + offset + 1
                print(f"[{index}/{total}] {job.key}", flush=True)
                scale = job_render_scale(job, args)
                result = finalize_capture(job, capture_id, frame_dir, target, args, server, scale, chrome)
                if not result.get("ok") and is_webgl_context_error(result.get("message")):
                    print("  WebGL context error; restarting Chrome and retrying once...", flush=True)
                    terminate_process(chrome)
                    chrome = launch_chrome(args, url, tmp_dir / f"chrome-controller-retry-{index}")
                    shutil.rmtree(frame_dir, ignore_errors=True)
                    frame_dir.mkdir(parents=True)
                    server.jobs.put(job_descriptor(capture_id, job, args, scale))
                    result = finalize_capture(job, capture_id, frame_dir, target, args, server, scale, chrome)
                if result.get("ok"):
                    manifest["jobs"].append(result)
                else:
                    manifest["errors"].append(result)
                # Raw RGBA frames are large; drop each job's frames once encoded so peak
                # disk stays near one job's worth instead of the whole run's.
                if not args.keep_temp:
                    shutil.rmtree(frame_dir, ignore_errors=True)
            # A single long-lived Chrome/WebGL context slowly exhausts GPU resources
            # over a large batch (observed failing with "does not support WebGL"
            # after roughly a dozen large liveevent captures). Recycling the process
            # between chunks keeps each context's lifetime bounded.
            terminate_process(chrome)
            chrome = None
            chunk_start = chunk_end
    finally:
        if chrome is not None:
            terminate_process(chrome)
        for _, capture_id, _, _ in plans:
            server.frame_dirs.pop(capture_id, None)


def finalize_capture(
    job: ExportJob,
    capture_id: str,
    frame_dir: Path,
    target: Path,
    args: argparse.Namespace,
    server: ExportServer,
    scale: int,
    chrome: subprocess.Popen[bytes],
) -> dict[str, Any]:
    fps = args.fps
    alpha = job_uses_alpha(job, args)
    try:
        capture_result = wait_for_capture(server, capture_id, args.capture_timeout, chrome)
        if not capture_result.get("ok"):
            raise RuntimeError(capture_result.get("message", "browser capture failed"))
        frame_count = int(capture_result.get("frames") or 0)
        if frame_count <= 0:
            raise RuntimeError("browser did not report exported frames")
        missing = missing_frames(frame_dir, frame_count)
        if missing:
            raise RuntimeError(f"browser export missed frame(s): {format_missing_frames(missing)}")
        frame_width = int(capture_result.get("width") or 0)
        frame_height = int(capture_result.get("height") or 0)
        if frame_width <= 0 or frame_height <= 0:
            raise RuntimeError("browser did not report frame dimensions")

        if job.group == "liveevent":
            # The browser picks its own render scale per job now (the largest
            # that avoids GL clamping -- see chooseLiveeventRenderScale in
            # spine-preview.html), which can differ from the static ceiling
            # this process requested. The crop-to-final-size division below
            # must use whatever scale was actually applied, or the math goes
            # wrong and the output comes out the wrong size.
            reported_scale = capture_result.get("scale")
            effective_scale = int(reported_scale) if reported_scale else scale
            return finalize_png_capture(
                job, frame_dir, target, frame_width, frame_height, frame_count, args, effective_scale, alpha
            )

        crop = None
        target_width = job.final_width
        target_height = job.final_height
        if should_crop_job(job, args):
            crop = detect_frame_crop(
                frame_dir,
                frame_count,
                frame_width,
                frame_height,
                scaled_crop_padding(job, args, scale),
                alpha,
                args.opaque_crop_threshold,
            )
            if crop is not None:
                target_width, target_height = scaled_crop_dimensions(crop, scale)

        start_frame = 0
        if job.group == "card" and not args.no_card_trim:
            start_frame = detect_card_lead_in(frame_dir, frame_count, frame_width, frame_height)

        capture_path = frame_dir.parent / f"{capture_id}.webm"
        encode_frames(frame_dir, capture_path, args, frame_width, frame_height, target_width, target_height, frame_count, crop, fps, fps, alpha, start_frame)
        output_width, output_height = video_dimensions(capture_path)
        actual_duration = video_duration(capture_path)

        shutil.copyfile(capture_path, target)
        return {
            "ok": True,
            "key": job.key,
            "character_id": job.character.id,
            "character": job.character.name,
            "group": job.group,
            "source_json": rel(job.source_json),
            "source_atlas": rel(job.source_atlas),
            "animations": list(job.animations),
            "intrinsic_dimensions": [job.intrinsic_width, job.intrinsic_height],
            "dimensions": [output_width, output_height],
            "planned_dimensions": [job.final_width, job.final_height],
            "capture_dimensions": list(actual_capture_dimensions(job, scale)),
            "requested_render_scale": render_scale(args),
            "render_scale": scale,
            "fps": fps,
            "capture_fps": fps,
            "output_fps": fps,
            "capture_mode": "deterministic",
            "actual_duration_seconds": round(actual_duration, 4),
            "frame_count": frame_count,
            "lead_in_trimmed": start_frame,
            "crop": dataclasses.asdict(crop) if crop else None,
            "duration_seconds": round(job.duration_seconds, 4),
            "output": rel(target),
        }
    except Exception as exc:  # noqa: BLE001 - manifest should record all job failures.
        return error_payload(job, str(exc))


def encode_liveevent_frame(
    job: ExportJob,
    frame_dir: Path,
    target: Path,
    frame_width: int,
    frame_height: int,
    frame_index: int,
    args: argparse.Namespace,
    scale: int,
    alpha: bool,
) -> tuple[VideoCrop | None, tuple[int, int]]:
    crop = None
    target_width = job.final_width
    target_height = job.final_height
    if should_crop_job(job, args):
        crop = detect_frame_crop(
            frame_dir,
            1,
            frame_width,
            frame_height,
            scaled_crop_padding(job, args, scale),
            alpha,
            args.opaque_crop_threshold,
            start_frame=frame_index,
        )
        if crop is not None:
            target_width, target_height = scaled_crop_dimensions(crop, scale)

    encode_png_frame(
        frame_dir, target, frame_width, frame_height, target_width, target_height, crop, alpha, start_frame=frame_index
    )
    return crop, video_dimensions(target)


def finalize_png_capture(
    job: ExportJob,
    frame_dir: Path,
    target: Path,
    frame_width: int,
    frame_height: int,
    frame_count: int,
    args: argparse.Namespace,
    scale: int,
    alpha: bool,
) -> dict[str, Any]:
    try:
        # Companion layers (if any) yield a second frame: frame 0 is the character
        # alone, frame 1 is the full bg_b/character/bg_f/eff composite. The
        # composite keeps the canonical output filename; the solo render is saved
        # alongside it so a broken composite (bad layer order, misaligned stage
        # frame) is easy to spot by comparing the two.
        output_solo = None
        if frame_count >= 2:
            solo_target = target.with_name(f"{target.stem}_solo{target.suffix}")
            encode_liveevent_frame(job, frame_dir, solo_target, frame_width, frame_height, 0, args, scale, alpha)
            output_solo = rel(solo_target)
            crop, (output_width, output_height) = encode_liveevent_frame(
                job, frame_dir, target, frame_width, frame_height, 1, args, scale, alpha
            )
        else:
            crop, (output_width, output_height) = encode_liveevent_frame(
                job, frame_dir, target, frame_width, frame_height, 0, args, scale, alpha
            )
        return {
            "ok": True,
            "key": job.key,
            "character_id": job.character.id,
            "character": job.character.name,
            "group": job.group,
            "source_json": rel(job.source_json),
            "source_atlas": rel(job.source_atlas),
            "animations": list(job.animations),
            "intrinsic_dimensions": [job.intrinsic_width, job.intrinsic_height],
            "dimensions": [output_width, output_height],
            "planned_dimensions": [job.final_width, job.final_height],
            "capture_dimensions": [frame_width, frame_height],
            "render_scale": scale,
            "capture_mode": "single_frame_png",
            "crop": dataclasses.asdict(crop) if crop else None,
            "frame_count": frame_count,
            "output": rel(target),
            "output_solo": output_solo,
            "background_layers": [
                {
                    "role": layer.role,
                    "index": layer.index,
                    "source_json": rel(layer.source_json),
                    "animation": layer.animation,
                    "rendered": layer.rendered,
                }
                for layer in job.layers
            ],
            "stage_frame": list(job.stage_frame) if job.stage_frame else None,
        }
    except Exception as exc:  # noqa: BLE001 - manifest should record all job failures.
        return error_payload(job, str(exc))


def runtime_urls(args: argparse.Namespace) -> list[str]:
    if not args.spine_runtime_url:
        return DEFAULT_RUNTIME_URLS
    urls = []
    for value in args.spine_runtime_url:
        path = Path(value)
        if path.exists():
            urls.append(path.resolve().as_uri())
        else:
            urls.append(value)
    return urls


def build_controller_url(base_url: str, runtime_url_list: list[str]) -> str:
    params = {
        "export": "1",
        "controller": "1",
        "runtime": "|".join(runtime_url_list),
    }
    return base_url + "/spine-preview.html?" + urllib.parse.urlencode(params)


def job_descriptor(capture_id: str, job: ExportJob, args: argparse.Namespace, scale: int) -> dict[str, Any]:
    return {
        "id": capture_id,
        "group": job.group,
        "json": rel(job.source_json),
        "atlas": rel(job.source_atlas),
        "anim": job.animations[0] if job.animations else "",
        "animations": list(job.animations),
        "width": job.capture_width,
        "height": job.capture_height,
        "fps": args.fps,
        "duration": round(job.duration_seconds, 4),
        "scale": str(scale),
        "transparent": job_uses_alpha(job, args),
        "layers": [
            {
                "role": layer.role,
                "index": layer.index,
                "json": rel(layer.source_json),
                "atlas": rel(layer.source_atlas),
                "animation": layer.animation,
            }
            for layer in job.layers
            if layer.rendered
        ],
        "stage_frame": (
            {
                "x": job.stage_frame[0],
                "y": job.stage_frame[1],
                "width": job.stage_frame[2],
                "height": job.stage_frame[3],
            }
            if job.stage_frame
            else None
        ),
        "output_scale": args.output_scale,
        "padding": args.padding,
        "liveevent_max_edge": args.liveevent_max_edge,
    }


def launch_chrome(args: argparse.Namespace, url: str, user_data_dir: Path) -> subprocess.Popen[bytes]:
    command = [
        args.chrome,
        "--headless=new",
        "--enable-webgl",
        "--ignore-gpu-blocklist",
        "--enable-gpu-rasterization",
        "--enable-zero-copy",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        f"--user-data-dir={user_data_dir}",
        "--window-size=1280,960",
    ]
    if args.force_swiftshader:
        command.extend([
            "--enable-unsafe-swiftshader",
            "--use-gl=swiftshader",
            "--use-angle=swiftshader",
        ])
    command.append(url)
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def missing_frames(frame_dir: Path, frame_count: int) -> list[int]:
    return [index for index in range(frame_count) if not (frame_dir / f"frame_{index:06d}.rgba").exists()]


def format_missing_frames(frames: list[int]) -> str:
    if len(frames) <= 10:
        return ", ".join(str(frame) for frame in frames)
    return ", ".join(str(frame) for frame in frames[:10]) + f", ... {len(frames) - 10} more"


def render_scale(args: argparse.Namespace) -> int:
    return int(args.render_scale)


def job_render_scale(job: ExportJob, args: argparse.Namespace) -> int:
    """Liveevent PNGs are a single still frame, so there's no per-frame speed cost to
    amortize; always supersample at 4x for the sharpest possible downsample."""
    if job.group == "liveevent":
        return 4
    return render_scale(args)


def output_extension(job: ExportJob) -> str:
    return ".png" if job.group == "liveevent" else ".webm"


def actual_capture_dimensions(job: ExportJob, scale: int) -> tuple[int, int]:
    return job.capture_width * scale, job.capture_height * scale


def should_crop_job(job: ExportJob, args: argparse.Namespace) -> bool:
    if job.group == "battle_ready":
        return not args.no_battle_crop
    if job.group == "card":
        return not args.no_card_crop
    if job.group == "liveevent":
        return not args.no_liveevent_crop
    return False


def job_uses_alpha(job: ExportJob, args: argparse.Namespace) -> bool:
    return job.group != "battle_ready" or args.battle_transparent


def scaled_crop_padding(job: ExportJob, args: argparse.Namespace, scale: int) -> int:
    if job.group == "battle_ready":
        padding = args.battle_crop_padding
    elif job.group == "liveevent":
        padding = args.liveevent_crop_padding
    else:
        padding = args.card_crop_padding
    return int(round(padding * scale))


def scaled_crop_dimensions(crop: VideoCrop, scale: int) -> tuple[int, int]:
    return even_floor(crop.width / scale), even_floor(crop.height / scale)


def raw_input_args(width: int, height: int, fps: int) -> list[str]:
    return [
        "-f",
        "rawvideo",
        "-pixel_format",
        "rgba",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        str(max(1, int(fps))),
    ]


def feed_raw_frames(stdin: Any, frame_dir: Path, count: int, start: int = 0) -> None:
    try:
        for index in range(start, count):
            stdin.write((frame_dir / f"frame_{index:06d}.rgba").read_bytes())
    except BrokenPipeError:
        pass
    finally:
        with contextlib.suppress(BrokenPipeError, OSError):
            stdin.close()


def run_ffmpeg_raw(input_args: list[str], frame_dir: Path, count: int, output_args: list[str], start: int = 0) -> tuple[int, str]:
    """Pipe frame files `[start, count)` of raw RGBA into ffmpeg in order.

    The browser now hands us raw GPU pixels (no PNG), so ffmpeg ingests them as a
    rawvideo stream over stdin. A writer thread streams the frames while the main
    thread drains stderr, so neither pipe can deadlock on a full buffer.
    """
    command = ["ffmpeg", "-hide_banner", "-y", *input_args, "-i", "-", *output_args]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    writer = threading.Thread(target=feed_raw_frames, args=(proc.stdin, frame_dir, count, start), daemon=True)
    writer.start()
    stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
    returncode = proc.wait()
    writer.join(timeout=5)
    return returncode, stderr


def encode_frames(
    frame_dir: Path,
    target: Path,
    args: argparse.Namespace,
    frame_width: int,
    frame_height: int,
    width: int,
    height: int,
    frame_count: int,
    crop: VideoCrop | None = None,
    capture_fps: int | None = None,
    output_fps: int | None = None,
    alpha: bool = True,
    start_frame: int = 0,
) -> None:
    filters = encode_filters(width, height, crop, output_fps or args.fps, alpha)
    output_args = [
        "-vf",
        filters,
        "-an",
        "-c:v",
        "libvpx-vp9",
        *vp9_encoder_options(args),
        "-pix_fmt",
        "yuva420p" if alpha else "yuv420p",
        "-auto-alt-ref",
        "0",
        "-b:v",
        "0",
        "-crf",
        str(args.vp9_crf),
    ]
    if alpha:
        output_args.extend(["-metadata:s:v:0", "alpha_mode=1"])
    output_args.append(str(target))
    input_args = raw_input_args(frame_width, frame_height, capture_fps or args.fps)
    returncode, stderr = run_ffmpeg_raw(input_args, frame_dir, frame_count, output_args, start_frame)
    if returncode != 0:
        raise RuntimeError("ffmpeg frame encode failed: " + stderr.strip())
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("ffmpeg frame encode produced an empty file")


def encode_png_frame(
    frame_dir: Path,
    target: Path,
    frame_width: int,
    frame_height: int,
    width: int,
    height: int,
    crop: VideoCrop | None,
    alpha: bool,
    start_frame: int = 0,
) -> None:
    filters = encode_filters(width, height, crop, None, alpha)
    output_args = [
        "-vf",
        filters,
        "-frames:v",
        "1",
        "-update",
        "1",
        "-pix_fmt",
        "rgba" if alpha else "rgb24",
        str(target),
    ]
    input_args = raw_input_args(frame_width, frame_height, 1)
    returncode, stderr = run_ffmpeg_raw(input_args, frame_dir, start_frame + 1, output_args, start_frame)
    if returncode != 0:
        raise RuntimeError("ffmpeg png encode failed: " + stderr.strip())
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("ffmpeg png encode produced an empty file")


def encode_filters(
    width: int, height: int, crop: VideoCrop | None = None, fps: int | None = None, alpha: bool = True
) -> str:
    # vflip undoes the bottom-up orientation of gl.readPixels before any crop/scale.
    filters = ["vflip"]
    if crop is not None:
        filters.append(f"crop={crop.width}:{crop.height}:{crop.x}:{crop.y}")
    if fps is not None:
        filters.append(f"fps={max(1, int(fps))}")
    filters.append(f"scale={width}:{height}:flags=lanczos")
    # rawvideo has no SAR, so it defaults to the unknown/degenerate 0:1 sentinel.
    # The VP9 muxer normalizes this to 1:1 on its own, but the PNG encoder writes
    # it verbatim into a pHYs chunk (x_res=0, y_res=1) -- some image viewers read
    # that as "non-square pixels" and rescale the displayed image accordingly,
    # which looks exactly like the pixel data itself being horizontally squashed
    # even though it isn't. Force square pixels explicitly for both output types.
    filters.append("setsar=1")
    # Spine renders premultiplied; scale in that space (clean edges) then convert
    # back to straight alpha for the yuva420p webm. Without this, semi-transparent
    # pixels (e.g. blush) keep their premultiplied RGB and show wrong colors.
    if alpha:
        filters.append("unpremultiply=inplace=1")
    return ",".join(filters)


def vp9_encoder_options(args: argparse.Namespace) -> list[str]:
    options = [
        "-deadline",
        args.vp9_deadline,
        "-cpu-used",
        str(args.vp9_cpu_used),
        "-row-mt",
        "1",
    ]
    if args.ffmpeg_threads > 0:
        options.extend(["-threads", str(args.ffmpeg_threads)])
    return options


def detect_frame_crop(
    frame_dir: Path,
    frame_count: int,
    width: int,
    height: int,
    padding: int,
    alpha: bool,
    opaque_threshold: int,
    start_frame: int = 0,
) -> VideoCrop | None:
    if not alpha:
        return detect_opaque_frame_crop(frame_dir, frame_count, width, height, padding, opaque_threshold, start_frame)
    sample = min(180, frame_count)
    input_args = raw_input_args(width, height, 30)
    output_args = ["-vf", "vflip," + cropdetect_filter(alpha), "-frames:v", str(sample), "-f", "null", "-"]
    returncode, stderr = run_ffmpeg_raw(input_args, frame_dir, start_frame + sample, output_args, start_frame)
    if returncode != 0:
        raise RuntimeError("ffmpeg frame crop detection failed: " + stderr.strip())
    return detect_crop_from_output(width, height, stderr, padding)


def detect_card_lead_in(frame_dir: Path, frame_count: int, width: int, height: int) -> int:
    """Return the first frame where a zoom-in card has reached its settled size.

    Card animations zoom the art in from small to full size. With a single fixed
    crop the smaller intro frames leave a transparent margin that players render as
    black, so we drop those lead-in frames and start playback at the settled card.
    """
    import numpy as np

    if frame_count <= 2:
        return 0
    widths = np.zeros(frame_count, dtype=np.int32)
    heights = np.zeros(frame_count, dtype=np.int32)
    for index in range(frame_count):
        alpha = np.frombuffer((frame_dir / f"frame_{index:06d}.rgba").read_bytes(), np.uint8)
        alpha = alpha.reshape(height, width, 4)[:, :, 3]
        cols = np.where(np.any(alpha > 16, axis=0))[0]
        rows = np.where(np.any(alpha > 16, axis=1))[0]
        if cols.size and rows.size:
            widths[index] = cols[-1] - cols[0] + 1
            heights[index] = rows[-1] - rows[0] + 1
    half = frame_count // 2
    target_w = float(np.median(widths[half:]))
    target_h = float(np.median(heights[half:]))
    if target_w <= 0 or target_h <= 0:
        return 0
    settled = np.where((widths >= 0.97 * target_w) & (heights >= 0.97 * target_h))[0]
    if settled.size == 0:
        return 0
    start = int(settled[0])
    # Never gut the clip: if the card only plateaus past the midpoint (continuous
    # zoom, no settled hold), leave it untrimmed rather than drop most of it.
    if start > half:
        return 0
    return start


def detect_opaque_frame_crop(
    frame_dir: Path,
    frame_count: int,
    width: int,
    height: int,
    padding: int,
    threshold: int,
    start_frame: int = 0,
) -> VideoCrop | None:
    similarity = max(0.0, min(1.0, max(0, int(threshold)) / 255.0))
    sample = min(180, frame_count)
    input_args = raw_input_args(width, height, 30)
    filters = f"vflip,colorkey=0x0f121d:similarity={similarity:.4f}:blend=0,format=rgba,alphaextract,cropdetect=limit=1:round=2:reset=0:skip=0"
    output_args = ["-vf", filters, "-frames:v", str(sample), "-f", "null", "-"]
    returncode, stderr = run_ffmpeg_raw(input_args, frame_dir, start_frame + sample, output_args, start_frame)
    if returncode != 0:
        raise RuntimeError("ffmpeg opaque frame crop detection failed: " + stderr.strip())
    return detect_crop_from_output(width, height, stderr, padding)


def cropdetect_filter(alpha: bool) -> str:
    # skip=0 disables cropdetect's default "skip the first 2 frames" behavior, which
    # would silently produce zero crop evaluations for a single-frame PNG capture.
    if alpha:
        return "format=rgba,alphaextract,cropdetect=limit=1:round=2:reset=0:skip=0"
    return "cropdetect=limit=40:round=2:reset=0:skip=0"


def detect_crop_from_output(width: int, height: int, output: str, padding: int) -> VideoCrop | None:
    bounds: list[tuple[int, int, int, int]] = []
    for match in re.finditer(r"x1:(\d+)\s+x2:(\d+)\s+y1:(\d+)\s+y2:(\d+)", output):
        x1, x2, y1, y2 = (int(group) for group in match.groups())
        if x2 > x1 and y2 > y1:
            bounds.append((x1, x2, y1, y2))
    if not bounds:
        return None

    return crop_from_bounds(width, height, bounds, padding)


def crop_from_bounds(width: int, height: int, bounds: list[tuple[int, int, int, int]], padding: int) -> VideoCrop | None:
    if not bounds:
        return None
    pad = max(0, int(padding))
    x1 = max(0, min(item[0] for item in bounds) - pad)
    x2 = min(width - 1, max(item[1] for item in bounds) + pad)
    y1 = max(0, min(item[2] for item in bounds) - pad)
    y2 = min(height - 1, max(item[3] for item in bounds) + pad)
    crop_width = even_floor(x2 - x1 + 1)
    crop_height = even_floor(y2 - y1 + 1)
    x = clamp_even(x1, 0, max(0, width - crop_width))
    y = clamp_even(y1, 0, max(0, height - crop_height))

    if crop_width >= width and crop_height >= height:
        return None
    return VideoCrop(x=x, y=y, width=crop_width, height=crop_height, source_width=width, source_height=height)


def video_dimensions(path: Path) -> tuple[int, int]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(path),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("ffprobe failed: " + result.stderr.strip())
    match = re.search(r"(\d+)x(\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"Could not read video dimensions for {path}")
    return int(match.group(1)), int(match.group(2))


def video_duration(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("ffprobe duration failed: " + result.stderr.strip())
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        duration = packet_duration(path)
    if duration <= 0:
        raise RuntimeError(f"Video duration was not positive for {path}")
    return duration


def packet_duration(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError("ffprobe packet duration failed: " + result.stderr.strip())
    end_time = 0.0
    for line in result.stdout.splitlines():
        values = [part.strip() for part in line.split(",") if part.strip() and part.strip() != "N/A"]
        if not values:
            continue
        try:
            pts = float(values[0])
            frame_duration = float(values[1]) if len(values) > 1 else 0.0
        except ValueError:
            continue
        end_time = max(end_time, pts + frame_duration)
    if end_time <= 0:
        raise RuntimeError(f"Could not read video duration for {path}")
    return end_time


def even_floor(value: int) -> int:
    number = max(2, int(value))
    return number if number % 2 == 0 else number - 1


def clamp_even(value: int, minimum: int, maximum: int) -> int:
    clamped = max(minimum, min(maximum, int(value)))
    if clamped % 2 == 0:
        return clamped
    return clamped - 1 if clamped > minimum else clamped + 1


def wait_for_capture(
    server: ExportServer,
    job_id: str,
    timeout: float,
    chrome: subprocess.Popen[bytes] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ok": False, "id": job_id, "message": f"timed out after {timeout:.1f}s"}
        if chrome is not None and chrome.poll() is not None:
            return {"ok": False, "id": job_id, "message": f"Chrome exited early (code {chrome.returncode})"}
        try:
            result = server.results.get(timeout=min(0.5, remaining))
        except queue.Empty:
            continue
        if result.get("id") == job_id:
            return result
        server.results.put(result)
        time.sleep(0.05)


def error_payload(job: ExportJob, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "key": job.key,
        "character_id": job.character.id,
        "character": job.character.name,
        "group": job.group,
        "source_json": rel(job.source_json),
        "animations": list(job.animations),
        "message": message,
    }


def is_webgl_context_error(message: str | None) -> bool:
    return bool(message) and "does not support WebGL" in message


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def write_manifest(manifest: dict[str, Any]) -> None:
    L2D_ROOT.mkdir(parents=True, exist_ok=True)
    path = L2D_ROOT / "export_manifest.json"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def print_summary(manifest: dict[str, Any]) -> None:
    print(
        "Done: "
        f"{len(manifest['jobs'])} exported, "
        f"{len(manifest['skipped'])} skipped, "
        f"{len(manifest['errors'])} errors."
    )
    if manifest["errors"]:
        print("First errors:")
        for item in manifest["errors"][:10]:
            print(f"- {item.get('key')}: {item.get('message')}")


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


if __name__ == "__main__":
    raise SystemExit(main())
