# AGENTS.md — maintaining regulus-modules

A catalogue of Regulus local modules, in Python and in Rust, and `catalog.py`, the small
tool that installs them into a project. Read README.md (what users see) and CONTRIBUTING.md (how a
module is added) before changing anything.

## The contract comes from Regulus

Regulus defines what a local module is; this repository only follows it. Reference:
Regulus `docs/03-modules.md` §6 (protocol, trust, `[[modules.local]]`),
`docs/18-custom-nodes.md` (SDK, `node init`, reload), and
`crates/regulus-core/src/authoring.rs` (how `regulus.toml` is written: append, check that
the parsed result is the old table plus the entry, replace in one rename). Do not invent
fields, commands or core APIs. Regulus has no `module install` command: do not say it does.

## Rules

- `catalog.py`: Python 3.11+, standard library only, one file. It never runs or builds
  module code (only the SDK's static `inspect` in `show`/`check`; a Rust module's nodes are
  read from its `regulus-module.toml`), never uses the network, never trusts a project and
  never writes outside `tools/<id>/` and the marked block of `regulus.toml`. `remove`
  deletes only what the receipt lists, unchanged, plus what was derived from it
  (`__pycache__`, a Rust module's `target/`). Keep it that way; no `--force` until someone
  needs it.
- One copy of each SDK: `sdk/regulus.py` (Python) and `sdk/regulus.rs` (the Rust helper),
  unchanged from Regulus, with version, sha256 and origin in `catalog.toml` and attribution
  in NOTICE. Modules never carry their own copy: a Rust module's `src/regulus.rs` is a
  link to `sdk/regulus.rs` in this clone, and install writes the real file.
- Python modules: standard library only, literal `@rg.node(...)` arguments, tests, a
  README that says what they touch. One purpose per module.
- Rust modules (`lang = "rust"` in `module.toml`): a crate as `regulus node init --lang
  rust` writes it (package `node-<id>`, its own `[workspace]`, `regulus-module.toml` as
  the manifest), `serde_json` as the only dependency unless there is a reason the README
  gives, `Cargo.lock` committed, tests in the crate that need no network. They are local
  modules over stdio, built in the project with `regulus node build`: never a crate
  linked into Regulus, and nothing prebuilt is shipped.
- English for code, docs and commits. Conventional Commits (`feat(modules): …`,
  `fix(catalog): …`, `docs: …`, `test: …`, `ci: …`). No `Co-Authored-By`, no `--no-verify`.
- Do not grow this into a registry, package manager or framework. A new feature needs a
  user who asked for it.

## Verify

```sh
python3 catalog.py check
for d in tests modules/*/tests template/tests; do python3 -m unittest discover -s "$d"; done
for m in modules/*/Cargo.toml; do cargo test --manifest-path "$m"; done   # Rust modules
# against a real core (isolated home and project; never the user's):
REGULUS_BIN=/path/to/regulus python3 -m unittest discover -s tests -p test_real_core.py -v
```

## Update the vendored SDK

Copy `sdk/python/regulus.py` from the Regulus repository at a released commit, set
`version`, `sha256` and `origin` (with that commit) in `catalog.toml`, update NOTICE and
the README's SDK version, run the checks above (the real-core one with that release), and
commit as `chore(sdk): vendor regulus sdk <version>`. The Rust helper is the same with
`sdk/rust/regulus.rs` and the `[sdk.rust]` table (its `version` is the Regulus release it
comes from: the file has none of its own).
