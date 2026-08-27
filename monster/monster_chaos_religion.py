from dataclasses import dataclass
from functools import cache

from utils.utils import load_db, load_text
from utils.wiki_utils import save_json_page

@dataclass
class Monster:
    id: str
    name: str
    type: str
    grade: str = ""
    desc: str = ""
    gimmick_title: str = ""
    gimmick_desc: str = ""

@cache
def parse_monsters() -> dict[str, Monster]:
    result = {}
    gimmick_map = {
        entry["id"]: entry
        for entry in load_db("monster(chaos_religion)@monster_gimmick")
    }

    for entry in load_db("monster(chaos_religion)@monster"):
        monster_id = entry["id"]
        name = entry["name"]
        if entry["grade"] == "GRADE_NORMAL":
            grade = entry["grade"].replace("GRADE_NORMAL", "Normal")
        elif entry["grade"] == "GRADE_ELITE":
            grade = entry["grade"].replace("GRADE_ELITE", "Elite")
        else:
            grade = entry["grade"]

        desc = load_text("chaos_collection_monster").get(f"monster_desc@ref@{monster_id}")

        gimmick_id = entry.get("link_monster_gimmick_id", "")
        gimmick = gimmick_map.get(gimmick_id, {})

        result[name] = Monster(
            id=monster_id,
            name=name,
            grade=grade,
            type="Chaos Religion",
            desc=desc,
            gimmick_title=gimmick.get("title_1", ""),
            gimmick_desc=gimmick.get("tip_1", ""),
        )

    return result

def save_monster_info():
    info_data = parse_monsters()
    save_json_page("Module:MonsterChaosReligion/data.json", info_data)

def main():
    save_monster_info()

if __name__ == "__main__":
    main()
