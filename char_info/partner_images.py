from char_info.partners import parse_partners
from utils.upload_utils import UploadRequest, process_uploads
from utils.utils import assets_root


def save_partner_portraits():
    partners = parse_partners()
    portrait_dir = assets_root / "face/character"
    uploads: list[UploadRequest] = []
    for partner_id, info in partners.items():
        source = portrait_dir / f"portrait_character_{partner_id}.png"
        if not source.exists():
            continue
        uploads.append(
            UploadRequest(
                source=source,
                target=f"{info.name} Portrait.png",
                text="{{FairUse}}\n[[Category:Partner portraits]]",
                summary="upload partner portrait",
            )
        )

    process_uploads(uploads, rename_duplicates=True)


def save_partner_card_illustrations():
    partners = parse_partners()
    illustration_dir = assets_root / "card_illustration"
    uploads: list[UploadRequest] = []
    for partner_id, info in partners.items():
        source = illustration_dir / f"support_{partner_id}_01.png"
        if not source.exists():
            continue
        uploads.append(
            UploadRequest(
                source=source,
                target=f"{info.name} Partner Card Illustration.png",
                text="{{FairUse}}\n[[Category:Partner card illustrations]]",
                summary="upload partner card illustration",
            )
        )

    process_uploads(uploads)


def save_partner_faces():
    partners = parse_partners()
    icon_dir = assets_root / "face" / "character"
    uploads: list[UploadRequest] = []
    for partner_id, info in partners.items():
        source = icon_dir / f"face_character_{partner_id}.png"
        if not source.exists():
            continue
        uploads.append(
            UploadRequest(
                source=source,
                target=f"Profile {info.name}.png",
                text="{{FairUse}}\n[[Category:Character profile pictures]]",
                summary="upload partner face image",
            )
        )

    process_uploads(uploads)


def upload_partner_images():
    save_partner_portraits()
    save_partner_card_illustrations()
    save_partner_faces()


def main():
    upload_partner_images()


if __name__ == "__main__":
    main()
