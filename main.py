from audio.audio_export import (
    export_combatant_voice_lines,
    export_partner_voice_lines,
)
from audio.wiki_audio_upload import (
    publish_combatant_voice_lines,
    publish_partner_voice_lines,
)
from char_info.autocreate_character_pages import (
    auto_create_combatant_pages,
    auto_create_counseling_pages,
)
from char_info.autocreate_partner_pages import auto_create_partner_pages
from char_info.cards import save_cards
from char_info.char_images import upload_character_images
from char_info.characters import save_character_info
from char_info.combatant_stats import update_combatant_stats_pages
from char_info.counseling import save_counseling
from char_info.ego_manifestation import save_ego_manifestations
from char_info.favourite_gifts import save_favourite_gifts
from char_info.partner_images import upload_partner_images
from char_info.partners import save_partner_info


def char_info():
    save_character_info()
    save_ego_manifestations()
    save_favourite_gifts()
    upload_character_images()
    auto_create_combatant_pages()
    update_combatant_stats_pages()
    # save_cards()
    save_counseling()
    auto_create_counseling_pages()


def partner_info():
    save_partner_info()
    upload_partner_images()
    auto_create_partner_pages()


def voice_lines():
    export_combatant_voice_lines()
    export_partner_voice_lines()
    publish_combatant_voice_lines()
    publish_partner_voice_lines()


def main():
    char_info()
    partner_info()
    voice_lines()


if __name__ == "__main__":
    main()
