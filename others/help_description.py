from dataclasses import dataclass
from functools import cache

from utils.utils import load_text
from utils.wiki_utils import save_json_page

@dataclass
class HelpDescription:
    id: str
    name: str
    desc: str

@cache
def parse_helpdescriptions() -> dict[str, HelpDescription]:
    result = {}
    helpdesc_text = load_text("tutorial_popup_page")
    for key, name in helpdesc_text.items():
        prefix, _, id_str = key.partition("@")
        if prefix == "title":
            if "combatant_6" in id_str:
                continue
            help_id = id_str
            desc = helpdesc_text.get(f'desc@{id_str}', "")   # default to empty string instead of None
            # desc.replace("<color_orange>", "")  need to assign result baco to the desc variable, otherwise it won't change the value of desc
            desc = desc.replace("<color_orange>", "").replace("</>", "")
            result[name] = HelpDescription(id=help_id, name=name, desc=desc)
    return result

def save_helpdescriptions_info():
    info_data = parse_helpdescriptions()
    save_json_page("Module:HelpDescription/data.json", info_data)

def main():
    save_helpdescriptions_info()

if __name__ == "__main__":
    main()