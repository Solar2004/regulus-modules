# text-extract

Agents answer in prose. A workflow usually needs one value out of that answer: the JSON
verdict a judge wrote inside a ```` ```json ```` block, or the script it put in a
```` ```python ```` block. `text-extract` takes the reply as text and sends on what is
inside it, so the next node gets data instead of a paragraph.

It complements the built-in `flow` module: `flow.convert` reads text that is *all* JSON
and fills `{{a.b}}` templates; `text-extract` finds the JSON or the code *inside* a longer
text.

| Node type | Input | Outputs | Fields |
|---|---|---|---|
| `extract_json` | `in` text@1 | `out` json@1 · `missing` text@1 | `path` (text, default `""`) |
| `code_blocks` | `in` text@1 | `out` text@1 (one value per block) · `count` json@1 | `language` (text, default `""`) · `pick` (`all` / `first` / `last`) |

## `extract_json`

Looks for the JSON value in this order and takes the first it finds:

1. the whole text, if it is JSON;
2. the first fenced code block marked `json` (or `jsonc`, `json5`) that parses, then the
   first unmarked one;
3. the first `{...}` object written in the prose, else the first `[...]` array.

`path` sends a part of it instead of the whole value: `scores.1` or `/scores/1` (a JSON
Pointer; `~1` is `/`, `~0` is `~`), read like the paths of the built-in `flow` rules.
When there is no JSON, or nothing at `path`, the text itself goes to `missing`, unchanged,
so a workflow can retry or ask again. A value that arrives already as JSON (not text) is
used as it is.

```
in:   Here is my verdict:
      ```json
      {"verdict": "pass", "scores": [7, 9]}
      ```
path = "verdict"   →  out: "pass"
path = "scores.1"  →  out: 9
path = "reason"    →  missing: (the whole text above)
```

## `code_blocks`

Sends the content of each fenced code block (```` ``` ```` or `~~~`, CommonMark rules: a
longer fence closes only with one at least as long; a block never closed runs to the end,
so a reply cut short still gives its code), then how many it sent on `count`.
`language` keeps only the blocks whose language (the first word after the opening fence)
is one of the listed names (`"python"`, `"sh, bash"`; case does not matter; empty keeps
every block). `pick` sends all of them, or only the first or the last.

```
in: Run this:
    ```sh
    make test
    ```
    then this:
    ```python
    print("ok")
    ```
language = "python"                  →  out: 'print("ok")\n'   count: 1
language = ""      pick = "first"    →  out: "make test\n"     count: 1
```

## In a project

```sh
python3 catalog.py install text-extract --project <project folder>
regulus project trust <project folder>
```

Its nodes then show up in the app's palette. In a profile file they are
`module_id = "<project id>/text-extract"` with `type = "extract_json"` or
`type = "code_blocks"`.

## Limits

- Reads only the text it gets: no files, commands or network.
- The prose scan tries at most 200 opening brackets per kind (`MAX_ATTEMPTS`), so a long
  text full of brackets that are not JSON ends in `missing` instead of a slow search.
- JSON is parsed by Python's `json` (standard JSON: no comments or trailing commas, even
  in a `jsonc` block).
- Tested with Regulus 0.2.1 (Python 3.11 and 3.14).
