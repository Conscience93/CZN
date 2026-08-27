from dataclasses import dataclass
from functools import cache

from utils.utils import load_text
from utils.wiki_utils import save_json_page

@dataclass
class Item:
    id: int
    name: str
    desc: str

@cache
def parse_items() -> dict[str, Item]:
    result = {}
    item_text = load_text("item")
    for key, name in item_text.items():
        prefix, _, id_str = key.partition("@")
        if prefix == "name":
            if id_str.isnumeric():
                item_id = int(id_str)
                desc = item_text.get(f'desc@{id_str}')
                result[name] = Item(id=item_id, name=name, desc=desc)
    return result

def save_item_info():
    info_data = parse_items()
    save_json_page("Module:Item/data.json", info_data)

def main():
    save_item_info()

if __name__ == "__main__":
    main()
