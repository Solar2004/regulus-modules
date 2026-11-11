# AGENTS.md — maintaining regulus-modules

A catalogue of Regulus local modules in Python, and `catalog.py`, the small tool that
installs them into a project. Read README.md (what users see) and CONTRIBUTING.md (how a
module is added) before changing anything.

## The contract comes from Regulus

Regulus defines what a local module is; this repository only follows it. Reference:
Regulus `docs/03-modules.md` §6 (protocol, trust, `[[modules.local]]`),
`docs/18-custom-nodes.md` (SDK, `node init`, reload), and
`crates/regulus-core/src/authoring.rs` (how `regulus.toml` is written: append, check that
the parsed result is the old table plus the entry, replace in one rename). Do not invent
fields, commands or core APIs. Regulus has no `module install` command: do not say it does.

## Rules

- `catalog.py`: Python 3.11+, standard library only, one file. It never runs module code
  (only the SDK's static `inspect` in `show`/`check`), never uses the network, never trusts
  a project and never writes outside `tools/<id>/` and the marked block of `regulus.toml`.
  `remove` deletes only what the receipt lists, unchanged. Keep it that way; no `--force`
  until someone needs it.
- One copy of the SDK: `sdk/regulus.py`, unchanged from Regulus, with version, sha256 and
  origin in `catalog.toml` and attribution in NOTICE. Modules never carry their own copy.
- Modules: pure Python, standard library only, literal `@rg.node(...)` arguments, tests,
  a README that says what they touch. One purpose per module.
- English for code, docs and commits. Conventional Commits (`feat(modules): …`,
  `fix(catalog): …`, `docs: …`, `test: …`, `ci: …`). No `Co-Authored-By`, no `--no-verify`.
- Do not grow this into a registry, package manager or framework. A new feature needs a
  user who asked for it.

## Verify

```sh
python3 catalog.py check
for d in tests modules/*/tests template/tests; do python3 -m unittest discover -s "$d"; done
# against a real core (isolated home and project; never the user's):
REGULUS_BIN=/path/to/regulus python3 -m unittest discover -s tests -p test_real_core.py -v
```

## Update the vendored SDK

Copy `sdk/python/regulus.py` from the Regulus repository at a released commit, set
`version`, `sha256` and `origin` (with that commit) in `catalog.toml`, update NOTICE and
the README's SDK version, run the checks above (the real-core one with that release), and
commit as `chore(sdk): vendor regulus sdk <version>`.
