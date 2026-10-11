"""Small text helpers."""

import re

_ENTITY = re.compile(r"&(#x[0-9a-f]+|#[0-9]+|amp|lt|gt|quot|apos);", re.IGNORECASE)
_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}


def decode_entities(text: str | None) -> str | None:
    """A title with its HTML entities turned back into text ("America&#39;s" -> "America's").

    A bill's title comes from a CMS, an API or a page attribute, and any of them may hand it over HTML-escaped; it is shown
    and sent as plain text, so an entity would appear as typed. Only numeric entities and the five named ones
    (`&amp; &lt; &gt; &quot; &apos;`), each with its semicolon, are decoded, in one pass; anything else ("AT&T", "&nbsp;",
    "&#0;") is left as it is. The chat widget applies the same rule (`decodeEntities` in `chat-widget/src/widget.js`).
    """
    if not text or "&" not in text:
        return text

    def decode(match: re.Match) -> str:
        entity = match.group(1).lower()
        if not entity.startswith("#"):
            return _NAMED[entity]
        try:
            point = int(entity[2:], 16) if entity[1] == "x" else int(entity[1:])
            return chr(point) if 0 < point <= 0x10FFFF else match.group(0)
        except (ValueError, OverflowError):
            return match.group(0)

    return _ENTITY.sub(decode, text)
