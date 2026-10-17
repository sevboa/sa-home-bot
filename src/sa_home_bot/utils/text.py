from __future__ import annotations


def clip(
    text: str,
    limit: int,
    *,
    mark: str = "",
    collapse_spaces: bool = False,
    at_word: bool = False,
) -> str:
    if collapse_spaces:
        text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if at_word:
        space = cut.rfind(" ")
        if space > limit // 2:
            cut = cut[:space]
    return cut.rstrip() + mark


def stem(word: str) -> str:
    if len(word) >= 6:
        return word[:-2]
    if len(word) >= 4:
        return word[:-1]
    return word
