from __future__ import annotations

import argparse
from pathlib import Path

from utils.upload_utils import UploadRequest, process_uploads
from utils.utils import assets_root, load_db


def icon_source(icon_name: str) -> Path:
    return assets_root / "img" / f"{icon_name}.png"


def build_class_icon_uploads() -> tuple[list[UploadRequest], list[str]]:
    uploads: list[UploadRequest] = []
    missing: list[str] = []

    for entry in load_db("base_class_define@base_class_define"):
        class_name = entry.get("class_name", "")
        icon_name = entry.get("class_icon", "")
        if not class_name or not icon_name:
            continue

        source = icon_source(icon_name)
        if not source.exists():
            missing.append(f"{class_name}: {source}")
            continue

        uploads.append(
            UploadRequest(
                source=source,
                target=f"{class_name} class icon.png",
                text="{{FairUse}}\n[[Category:Combat class icons]]",
                summary="upload combat class icon",
            )
        )

    return uploads, missing


def build_faction_icon_uploads(
    include_unresolved: bool = False,
) -> tuple[list[UploadRequest], list[str], list[str]]:
    uploads: list[UploadRequest] = []
    missing: list[str] = []
    skipped: list[str] = []

    for entry in load_db("faction@faction"):
        faction_name = entry.get("name", "")
        icon_name = entry.get("icon_small", "")
        if not faction_name or not icon_name:
            continue
        if faction_name.startswith("FACTION_") and not include_unresolved:
            skipped.append(f"{faction_name}: unresolved name")
            continue

        source = icon_source(icon_name)
        if not source.exists():
            missing.append(f"{faction_name}: {source}")
            continue

        uploads.append(
            UploadRequest(
                source=source,
                target=f"{faction_name} faction icon.png",
                text="{{FairUse}}\n[[Category:Faction icons]]",
                summary="upload faction icon",
            )
        )

    return uploads, missing, skipped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the upload plan without uploading files.",
    )
    parser.add_argument(
        "--include-unresolved-factions",
        action="store_true",
        help="Include faction rows whose names still look like FACTION_* text ids.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    uploads: list[UploadRequest] = []
    missing: list[str] = []
    skipped: list[str] = []

    class_uploads, class_missing = build_class_icon_uploads()
    faction_uploads, faction_missing, faction_skipped = build_faction_icon_uploads(
        include_unresolved=args.include_unresolved_factions,
    )
    uploads.extend(class_uploads)
    uploads.extend(faction_uploads)
    missing.extend(class_missing)
    missing.extend(faction_missing)
    skipped.extend(faction_skipped)

    print(f"Prepared {len(class_uploads)} combat class icon uploads.")
    print(f"Prepared {len(faction_uploads)} faction icon uploads.")

    if missing:
        print("Missing icon assets:")
        for item in missing:
            print(f"- {item}")

    if skipped:
        print("Skipped icon rows:")
        for item in skipped:
            print(f"- {item}")

    if args.dry_run:
        for upload in uploads:
            print(f"{upload.source} -> File:{upload.target}")
        return

    process_uploads(uploads)


if __name__ == "__main__":
    main()
