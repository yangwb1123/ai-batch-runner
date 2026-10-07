"""Conservative JSX label associations; no file-wide 'a label exists' exemption."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Tag:
    name: str
    start: int
    end: int
    attrs: str
    parent: Tag | None = None
    children: list[Tag] = field(default_factory=list)


def blank_comments(source: str) -> str:
    """Mask comments without altering quoted attribute values or line offsets."""
    pattern = r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|/\*.*?\*/|//[^\n]*'''
    def blank(match: re.Match) -> str:
        if match.group(1):
            return match.group(0)
        return "".join("\n" if c == "\n" else " " for c in match.group(0))
    return re.sub(pattern, blank, source, flags=re.S)


def _quote_state(char: str, quote: str, escaped: bool) -> tuple[str | None, bool]:
    """Consume quoted text without treating escaped quotes as terminators."""
    if escaped:
        return quote, False
    if char == "\\":
        return quote, True
    return (None if char == quote else quote), False


def tag_end(source: str, start: int) -> int:
    """Arrow operators/comparisons inside attribute expressions are not tag ends."""
    quote = None
    braces = 0
    escaped = False
    for cursor in range(start, len(source)):
        char = source[cursor]
        if quote:
            quote, escaped = _quote_state(char, quote, escaped)
            continue
        if char in "\"'`":
            quote = char
        elif char == "{":
            braces += 1
        elif char == "}":
            braces = max(0, braces - 1)
        elif char == ">" and braces == 0:
            return cursor + 1
    return len(source)


def attribute(attrs: str, name: str) -> str | None:
    match = re.search(r"(?:^|\s)" + re.escape(name) + r"\s*=\s*", attrs)
    if not match:
        return None
    tail = attrs[match.end():].lstrip()
    if tail.startswith(('"', "'")):
        value = re.match(r'''["']((?:\\.|[^"'])*)["']''', tail)
        return "literal:" + value.group(1) if value else None
    if tail.startswith("{"):
        depth = 0
        for index, char in enumerate(tail):
            depth += (char == "{") - (char == "}")
            if depth == 0:
                value = tail[1:index].strip()
                if re.fullmatch(r'''["'][^"']*["']''', value):
                    return "literal:" + value[1:-1]
                return "expression:" + value
    return None


def present(value: str | None) -> bool:
    return bool(value and value not in {"literal:", "expression:", "expression:null",
                                       "expression:undefined", "expression:false"})


def tags_in(source: str) -> list[Tag]:
    code = blank_comments(source)
    tags: list[Tag] = []
    stack: list[Tag] = []
    for match in re.finditer(r"<(\/?)([A-Za-z][\w.]*)\b", code):
        closing, name = match.groups()
        if closing:
            if stack and stack[-1].name == name:
                stack.pop()
            continue
        end = tag_end(code, match.end())
        tag = Tag(name, match.start(), end, code[match.end():end], stack[-1] if stack else None)
        if tag.parent:
            tag.parent.children.append(tag)
        tags.append(tag)
        if not code[match.end():end].rstrip().endswith("/>"):
            stack.append(tag)
    return tags


def associated_label(tag: Tag, tags: list[Tag], form_fields: set[str]) -> bool:
    parent = tag.parent
    while parent:
        if parent.name == "label":
            return True
        if parent.name == "IrisFormField" and tag.name in form_fields:
            # Vue clones only the first *direct* component child, not every
            # descendant or a second sibling. Check the actual composition.
            components = [child for child in parent.children if child.name[0].isupper()]
            if (tag.parent is parent and components and components[0] is tag
                    and present(attribute(parent.attrs, "label"))):
                return True
        parent = parent.parent
    identifier = attribute(tag.attrs, "id")
    return present(identifier) and any(
        other.name == "label" and attribute(other.attrs, "for") == identifier
        for other in tags
    )


def accessible_name_missing(
    source: str, controls: tuple[str, ...], routing: dict[str, bool],
    form_fields: set[str] | None = None, camel: dict[str, bool] | None = None,
) -> list[tuple[int, str]]:
    tags = tags_in(source)
    missing = []
    for tag in tags:
        if tag.name not in controls or associated_label(tag, tags, form_fields or set()):
            continue
        aria = present(attribute(tag.attrs, "aria-label")) or present(attribute(tag.attrs, "aria-labelledby"))
        camel_aria = present(attribute(tag.attrs, "ariaLabel"))
        if aria and routing.get(tag.name, True):
            continue
        if camel_aria and (camel or {}).get(tag.name, False):
            continue
        detail = tag.name
        if aria or camel_aria:
            detail += " 的 ARIA 未证明落在原生控件上，需使用已验证的命名通路（如 id + 匹配的 <label for>）"
        missing.append((source.count("\n", 0, tag.start) + 1, detail))
    return missing
