from string import Template

from char_info.partners import parse_partners, partner_pages
from utils.wiki_utils import save_wikitext_page


def auto_create_partner_pages():
    template = Template("""{{Partner Infobox|name=$name}}

'''$name''' is a partner in Chaos Zero Nightmare.

==Game Description==
{{Partner Description|name=$name}}

==Ego Skill==
{{Partner Ego Skill|name=$name}}

==Passive Skill==
{{Partner Passive Skill|name=$name}}

==Gallery==
{{Partner Gallery|name=$name}}

==Voice==

==Navigation==
{{Partner Navbox}}""")

    pages = {p.title(with_ns=False): p for p in partner_pages()}
    for partner in sorted(parse_partners().values(), key=lambda partner: partner.id):
        p = pages[partner.name]
        if p.exists():
            continue

        text = template.safe_substitute(name=partner.name)
        save_wikitext_page(p, text, summary="auto-create partner page")


def main():
    auto_create_partner_pages()


if __name__ == "__main__":
    main()
