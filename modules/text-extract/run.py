#!/usr/bin/env python3
"""text-extract: pull JSON values and fenced code blocks out of text, such as an agent's reply.

A Regulus local module written with the official Python SDK (`regulus.py`, installed
beside this file; standard library only). Two node types:

- `extract_json` finds the JSON value in a text: the whole text if it is JSON, else the
  first fenced code block that is (blocks marked `json` first), else the first object,
  then the first array, written in the prose. `path` sends only a part of it: `a.b.0`, or a
  JSON Pointer such as `/a/b/0`, read the way the built-in `flow` rules read paths. The
  value goes to `out` (json@1). A text without JSON, or whose JSON has nothing at `path`,
  goes to `missing` unchanged (text@1), so a workflow can route it elsewhere.
- `code_blocks` puts the content of each fenced code block on `out` (text@1), only the
  blocks whose language is listed in `language` (empty: every block), all of them or only
  the first or the last; then how many it sent on `count` (json@1).

Nothing here reads files, runs commands or opens the network. Regulus runs it only in a
project the user trusts, with the user's permissions: it is not a sandbox.
"""
import json
import re

import regulus as rg

# Characters that open a fenced code block, and the shortest fence (CommonMark).
FENCE_CHARS = "`~"
MIN_FENCE = 3
# A fence may be indented by up to this many spaces (CommonMark).
MAX_FENCE_INDENT = 3
# Languages of a fenced block read as JSON, besides blocks with no language.
JSON_LANGUAGES = ("json", "jsonc", "json5")
# Openers tried in prose before giving up: bounds the work on long text full of brackets
# that are not JSON.
MAX_ATTEMPTS = 200
PICKS = ["all", "first", "last"]
LANGUAGE_SEPARATORS = re.compile(r"[,\s]+")
INDEX = re.compile(r"0|[1-9][0-9]*")


class _Missing:
    """Nothing found: JSON's own `null` is a value, so it cannot say that."""

    def __repr__(self):
        return "MISSING"


MISSING = _Missing()


def _fence(line):
    """The fence a line opens or closes: (char, length, indent, info), or None."""
    text = line.rstrip("\r\n")
    indent = len(text) - len(text.lstrip(" "))
    if indent > MAX_FENCE_INDENT:
        return None
    rest = text[indent:]
    if not rest or rest[0] not in FENCE_CHARS:
        return None
    char = rest[0]
    length = len(rest) - len(rest.lstrip(char))
    if length < MIN_FENCE:
        return None
    return char, length, indent, rest[length:].strip()


def _unindent(line, indent):
    """`line` without up to `indent` leading spaces, as CommonMark does inside a fence."""
    spaces = len(line) - len(line.lstrip(" "))
    return line[min(spaces, indent):]


def fenced_blocks(text):
    """The fenced code blocks of a Markdown text, in order, as (language, content).

    `language` is the first word of the info string, lower-cased ("" if none); `content`
    keeps its line endings. A block that is never closed runs to the end of the text, as
    in CommonMark (a reply cut short still gives its code).
    """
    blocks = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        opening = _fence(lines[i])
        i += 1
        if opening is None:
            continue
        char, length, indent, info = opening
        if char == "`" and "`" in info:
            continue  # an inline code span, not a fence
        body = []
        while i < len(lines):
            closing = _fence(lines[i])
            i += 1
            if closing and closing[0] == char and closing[1] >= length and not closing[3]:
                break
            body.append(_unindent(lines[i - 1], indent))
        words = info.split()
        blocks.append((words[0].lower() if words else "", "".join(body)))
    return blocks


def _loads(text):
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return MISSING


def _in_prose(text):
    """The first JSON object written in `text`, else the first array, else MISSING."""
    decoder = json.JSONDecoder()
    for opener in "{[":
        start = 0
        for _ in range(MAX_ATTEMPTS):
            at = text.find(opener, start)
            if at < 0:
                break
            try:
                return decoder.raw_decode(text, at)[0]
            except (ValueError, RecursionError):
                start = at + 1
    return MISSING


def find_json(text):
    """The JSON value in `text` (see the module's docstring), or MISSING."""
    found = _loads(text.strip()) if text.strip() else MISSING
    if found is not MISSING:
        return found
    blocks = fenced_blocks(text)
    marked = [content for language, content in blocks if language in JSON_LANGUAGES]
    plain = [content for language, content in blocks if not language]
    for content in marked + plain:
        found = _loads(content)
        if found is not MISSING:
            return found
    return _in_prose(text)


def _unescape(token):
    return token.replace("~1", "/").replace("~0", "~")


def at_path(value, path):
    """What `path` points at in `value`, or MISSING.

    Empty or "." is the whole value; "/a/0" is a JSON Pointer; "a.0" is the same path
    written with dots (each part is a pointer token, as in the built-in `flow` rules).
    """
    path = path.strip()
    if path in ("", "."):
        return value
    tokens = path[1:].split("/") if path.startswith("/") else path.split(".")
    for token in map(_unescape, tokens):
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and INDEX.fullmatch(token) and int(token) < len(value):
            value = value[int(token)]
        else:
            return MISSING
    return value


def languages(setting):
    """The languages a `language` setting lists, lower-cased: "py, Python" is {py, python}."""
    return {word.lower() for word in LANGUAGE_SEPARATORS.split(setting) if word}


def pick_blocks(blocks, language, pick):
    """The contents of `blocks` in `language` (empty: any), all of them or first or last."""
    if pick not in PICKS:
        raise rg.InvalidInput(f"pick must be one of {', '.join(PICKS)}, not {pick!r}")
    wanted = languages(language)
    chosen = [content for lang, content in blocks if not wanted or lang in wanted]
    if pick == "first":
        return chosen[:1]
    if pick == "last":
        return chosen[-1:]
    return chosen


def _setting(ctx, field_id):
    value = ctx.config.get(field_id)
    return "" if value is None else str(value)


@rg.node(
    "extract_json",
    name="Extract JSON",
    inputs=[rg.then_in("in", "text@1")],
    outputs=[rg.then_out("out", "json@1"), rg.then_out("missing", "text@1")],
    fields=[
        rg.field(
            "path", "text", default="", label="Path",
            description="Part of the JSON to send: a.b.0 or a JSON Pointer such as "
                        "/a/b/0. Empty sends the whole value.",
        ),
    ],
)
def extract_json(ctx, port, value):
    """A text arrived: send its JSON (or the part at `path`) on `out`, else the text on
    `missing`. A value that is not text is taken as JSON already."""
    found = find_json(value) if isinstance(value, str) else value
    if found is not MISSING:
        found = at_path(found, _setting(ctx, "path"))
    if found is MISSING:
        ctx.emit("missing", value if isinstance(value, str) else json.dumps(value))
    else:
        ctx.emit("out", found)


@rg.node(
    "code_blocks",
    name="Code blocks",
    inputs=[rg.then_in("in", "text@1")],
    outputs=[rg.then_out("out", "text@1"), rg.then_out("count", "json@1")],
    fields=[
        rg.field(
            "language", "text", default="", label="Language",
            description="Only blocks in these languages, such as python or \"sh, bash\". "
                        "Empty takes every block.",
        ),
        rg.field(
            "pick", "select", default="all", options=PICKS, label="Pick",
            description="Send every block, or only the first or the last one.",
        ),
    ],
)
def code_blocks(ctx, port, value):
    """A text arrived: send each chosen code block on `out`, then how many on `count`."""
    if not isinstance(value, str):
        raise rg.InvalidInput("text@1 must be a string")
    chosen = pick_blocks(fenced_blocks(value), _setting(ctx, "language"), _setting(ctx, "pick"))
    for content in chosen:
        ctx.emit("out", content)
    ctx.emit("count", len(chosen))


if __name__ == "__main__":
    rg.run(name="Text extract")
