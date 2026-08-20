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

    if t == StoryElementType.BACKGROUND:
        name = _escape_wikitext(a.get("name", ""))
        return [f"{{{{StoryBackground|name={name}}}}}"]

    if t in (StoryElementType.CHAPTER, StoryElementType.LOCATION):
        text = _escape_wikitext(a.get("text", ""))
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

    if t == StoryElementType.COUNSELING:
        return ["{{StoryDialogueNarration|message='''Counseling'''}}"]

    return []


def scene_to_wikitext(scene: StoryScene) -> str:
    lines = []
    for element in scene.elements:
        lines.extend(element_to_wikitext(element))
    return "\n".join(line for line in lines if line is not None)


def episode_to_wikitext(episode: StoryEpisode) -> str:
    scenes = get_story_scenes()
    parts = []
    for sid in episode.scene_ids:
        scene = scenes[sid]
        wt = scene_to_wikitext(scene)
        if wt:
            parts.append(wt)
    # return "{{StoryTop}}\n{{StoryContainer|\n\n" + "\n{{StoryDialogueSeparator}}\n".join(parts) + "\n\n}}\n{{StoryBottom}}"
    return f"{{{{EpisodeDescription|name={episode.name}}}}}\n\n{{{{StoryContainer|\n\n" + "\n{{StoryDialogueSeparator}}\n".join(parts) + "\n\n}}"


def chapter_overview_wikitext(
    act: int,
    chapters: list[tuple[int, list[StoryEpisode], list[StoryEpisode]]],
) -> str:
    lines: list[str] = []
    for chapter_num, main_eps, sub_eps in chapters:
        if lines:
            lines.append("")
        lines.append(f"== Chapter {chapter_num} ==")
        lines.append(f"=== Description ===")
        lines.append(f"{{{{ChapterDescription}}}}")
        if main_eps:
            lines.append("=== Main story ===")
            for ep in main_eps:
                lines.append(f"* [[Story/Act {act}/{ep.name}|{ep.display_title}]]")
        if sub_eps:
            lines.append("=== Side stories ===")
            for ep in sub_eps:
                lines.append(f"* [[Story/Act {act}/{ep.name}|{ep.display_title}]]")
    return "\n".join(lines)
