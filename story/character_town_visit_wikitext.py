from story.story_localize import localize_name, PROTAGONIST_MARKERS
from story.story_parser import get_story_scenes
from story.story_types import StoryElement, StoryElementType, StoryEpisode, StoryScene


def _escape_wikitext(text: str) -> str:
    return text.replace("|", "{{!}}")


def _story_dialogue(text: str, talker: str = "") -> str:
    text = _escape_wikitext(text.strip())
    if talker and text:
        return f"{{{{StoryDialogue|name={talker}|message={text}}}}}"
    elif text == "":
        return None
    return f"{{{{StoryDialogueDefaultImage|message={text}}}}}"


def element_to_wikitext(element: StoryElement) -> list[str]:
    t = element.type
    a = element.args

    # if t == StoryElementType.BACKGROUND:
    #     name = _escape_wikitext(a.get("name", ""))
    #     if name in ("black", "dim", "dim_orenge", "dim_red"):
    #         return ""
    #     if name.startswith(("b_earth", "e_earth", "b_spaitz", "e_spaitz", "b_laino", "e_laino")):
    #         return ""
    #     else:
    #         return [f"{{{{StoryBackground|name={name}}}}}"]

    if t in (StoryElementType.CHAPTER, StoryElementType.LOCATION):
        text = _escape_wikitext(a.get("text", ""))
        if "<br><br>" in text:
            text = text.replace("<br><br>", ": ")
        return [f"{{{{StoryDialogueNarration|message='''{text}'''}}}}"]

    if t in (
        StoryElementType.NARRATION,
        StoryElementType.CAPTION,
        StoryElementType.ENDING,
    ):
        text = _escape_wikitext(a.get("text", ""))
        return [f"{{{{StoryDialogueNarration|message={text}}}}}"]

    if t == StoryElementType.DIALOGUE:
        talker = _escape_wikitext(localize_name(a.get("talker", "")))
        return [_story_dialogue(a.get("text", ""), talker)]

    if t == StoryElementType.PROTAGONIST:
        return [_story_dialogue(a.get("text", ""))]

    if t == StoryElementType.MONOLOGUE:
        talker = a.get("talker", "")
        talker = "" if talker in PROTAGONIST_MARKERS else _escape_wikitext(localize_name(talker))
        return [_story_dialogue(a.get("text", ""), talker)]

    if t == StoryElementType.CHOICE:
        text = a.get("text", "")
        options = [opt.strip() for opt in text.split("|")]
        lines = []
        for opt in options:
            escaped = _escape_wikitext(opt)
            lines.append(f"{{{{StoryDialoguePlayerChoice|message={escaped}}}}}")
        return lines

    return []


def scene_to_wikitext(scene: StoryScene) -> str:
    elements = scene.elements
    lines: list[str] = []
    i = 0
    while i < len(elements):
        elem = elements[i]
        if elem.type != StoryElementType.CHOICE:
            lines.extend(element_to_wikitext(elem))
            i += 1
            continue

        lines.extend(element_to_wikitext(elem))

        choice_keys = [k.strip() for k in elem.args.get("choice", "").split("|")]
        option_texts = [t.strip() for t in elem.args.get("text", "").split("|")]
        key_to_option = dict(zip(choice_keys, option_texts))

        i += 1
        branches: dict[str, list[StoryElement]] = {}
        branch_order: list[str] = []
        while i < len(elements):
            elem = elements[i]
            use_choice = elem.args.get("use_choice", "")
            if not use_choice:
                if elem.type == StoryElementType.BACKGROUND:
                    for key in branch_order:
                        branches[key].append(elem)
                    i += 1
                    continue
                break
            if use_choice not in branches:
                branches[use_choice] = []
                branch_order.append(use_choice)
            branches[use_choice].append(elem)
            i += 1

        for key in branch_order:
            label = key_to_option.get(key, key)
            heading = _escape_wikitext(f"<p style=\"color: orange; text-indent: 5em;\">Player Choice: {label}</p>")  
            lines.append(f"{{{{StoryDialogueNarration|message='''{heading}'''}}}}")
            for branch_elem in branches[key]:
                lines.extend(element_to_wikitext(branch_elem))

    return "\n".join(line for line in lines if line is not None)

