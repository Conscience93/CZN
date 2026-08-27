from dataclasses import dataclass
from functools import cache

from utils.utils import load_text
from utils.wiki_utils import save_json_page

@dataclass
class ChapterDescription:
    id: int
    name: str
    desc: str

@cache
def parse_chapterdescriptions() -> dict[str, ChapterDescription]:
    result = {}
    chapterdesc_text = load_text("nw_story_map_chapter")
    for key, name in chapterdesc_text.items():
        prefix, _, id_str = key.partition("@")
        if prefix == "name":
            chapter_id = id_str
            desc = chapterdesc_text.get(f'desc@{id_str}')
            result[name] = ChapterDescription(id=chapter_id, name=name, desc=desc)
    return result

def save_chapterdescriptions_info():
    info_data = parse_chapterdescriptions()
    save_json_page("Module:NewAct1ChapterDescription/data.json", info_data)

def main():
    save_chapterdescriptions_info()

if __name__ == "__main__":
    main()