# regulus-modules

Python modules for [Regulus](https://github.com/Solar2004/Regulus), ready to install into a
project, and the place to share your own.

Each module is a Regulus **local module** written with the official Python SDK: a
`run.py` that Regulus starts as a process and talks to over stdin/stdout. Installing one
copies it into your project's `tools/<id>/` and adds its `[[modules.local]]` entry to
`regulus.toml`, the same layout `regulus node init` creates. No server, no package
manager, no download at install time: you clone this repository, read what you install,
and run one command.

## Catalogue

| Module | Version | What it does | Node types |
|---|---|---|---|
| [`text-extract`](modules/text-extract/README.md) | 0.1.0 | Pulls the JSON value or the fenced code blocks out of a text, such as an agent's reply, so the next node gets data instead of prose. | `extract_json`, `code_blocks` |

`python3 catalog.py list` prints the same list from your clone, and
`python3 catalog.py show <id>` prints a module's ports, fields, files and checksums.

## Use a module

You need:

- **Regulus 0.2.1 or newer** (the CLI, `regulus`, or the desktop app).
- **An existing Regulus project**: a folder that contains a `regulus.toml`. `catalog.py`
  installs *into* a project and refuses a folder without that file; it does not create
  projects. Already have one? Use its folder and skip step 2.
- `git` and `python3` 3.11 or newer for `catalog.py` (the modules themselves need Python
  3.9 or newer and only the standard library).

Installing only copies files, so it needs no running core. Opening, trusting and
reloading a project are `regulus project …` commands, which talk to a **running core**.
In this example the project is `~/my-project`; its id is `my-project`.

```sh
# 1. Terminal A: start the core and leave it running.
regulus serve

# 2. Terminal B: make the folder a project. It must exist; this writes a minimal
#    regulus.toml if there is none, and does not trust the folder.
mkdir -p ~/my-project
regulus project open ~/my-project
regulus project ls                      # first column: the project id (my-project)

# 3. Get the catalogue and install a module into the project.
git clone https://github.com/Solar2004/regulus-modules
cd regulus-modules
python3 catalog.py show text-extract
python3 catalog.py install text-extract --project ~/my-project
```

The module does not run yet. Read `~/my-project/tools/text-extract/run.py`, then trust the
project and load the module:

```sh
# 4. Trusting lets the project's local modules run, as you; there is no sandbox.
regulus project trust ~/my-project
regulus project reload my-project --module text-extract
regulus module ls                       # my-project/text-extract@0.1.0   idle
```

The project id is the first column of `regulus project ls`: the folder's name, unless its
`regulus.toml` sets `[project] id`. Wherever these docs write `<project-id>`, put your
project's id there, without the angle brackets. Its nodes then show up in the app's palette
(module id `<project-id>/text-extract`). `install` prints the trust and reload commands
with your project's real path and id filled in, and never trusts a project for you.

**In the desktop app** instead of the CLI: open the folder there (**New Python node →
Open a project folder…** opens it untrusted, and writes a `regulus.toml` if there is
none), run the `catalog.py install` command from a terminal, then trust the project and
reload the module from **Settings → Modules**.

To remove it (`remove` only edits files, so it needs no core; the reload does):

```sh
python3 catalog.py remove text-extract --project ~/my-project
regulus project reload my-project       # if the core has the project open
```

## What install and remove do, and do not

- **Install** checks every file against the sha256 in the module's `module.toml` and the
  SDK copy against `catalog.toml`, then creates `tools/<id>/` (`run.py`, the SDK as
  `regulus.py`, and `.regulus-modules.json`, a receipt of what it wrote) and appends one
  marked block to `regulus.toml`:

  ```toml
  # >>> regulus-modules: text-extract >>>
  # Text extract 0.1.0, from the regulus-modules catalogue. ...
  [[modules.local]]
  id = "text-extract"
  command = ["python3", "tools/text-extract/run.py"]
  version = "0.1.0"
  # <<< regulus-modules: text-extract <<<
  ```

  It refuses, writing nothing, if `tools/<id>` exists, if `regulus.toml` already has an
  entry with that id, or if appending would change anything else the file says. Your
  comments, tables and other entries stay byte for byte. If a step fails, what it created
  is removed. It runs no module code.
- **Remove** deletes only what install wrote, and only if it is unchanged: the files must
  still match the receipt, the folder may hold nothing else (but Python's
  `__pycache__`), and the marked block must be intact. Otherwise it refuses and changes
  nothing, listing what differs. To keep an edited module, copy your changes out first,
  then delete `tools/<id>/` and the marked block by hand. Installing and then removing
  leaves `regulus.toml` exactly as it was.
- **Regulus has no `module install` command.** `catalog.py` is a small standalone tool of
  this repository; it does not talk to the core.

## Trust: these modules run as you

Opening a project never runs its code. Regulus starts a project's local modules only once
you trust the project (`regulus project trust <folder>`), and then they run **with your
user's permissions: there is no sandbox**. The core only empties their environment (by
default they get `PATH`, `HOME`, `LANG`, `TERM` and the entry's `env`; never the core's
token or your secrets). So read a module's `run.py` before you trust the project, as you
would any script. A module's README says what it touches outside its process.

The checksums prove the files you install are the ones the catalogue lists, not that they
are safe; they come from the same repository. Review is what makes a module safe to run.

## Custom Python modules and built-in Rust modules

| | Built-in modules (`flow`, `files`, `agent`…) | Modules in this catalogue |
|---|---|---|
| Written in | Rust, linked into the `regulus` binary | Python, with the official SDK |
| Comes with | Regulus itself | a clone of this repository |
| Runs | inside the core | as a separate process per project |
| Needs a trusted project | no | **yes** |
| What they can use of the core | all of it (`ModuleCtx`) | `emit` only: "then" ports, no "uses" ports |
| Add or change one | edit Regulus and rebuild | install, edit `run.py`, `project reload` |

A Python module cannot replace a built-in one; it adds node types next to them.

## Write and share your own

For a node only you need, `regulus node init <project> --id <id>` writes one straight
into your project. To share it here, so anyone can install it with one command, follow
[CONTRIBUTING.md](CONTRIBUTING.md): copy `template/`, write the node, add tests and a
README, run `python3 catalog.py check`, add a row above and open a pull request.

## Repository layout

```
catalog.py               list · show · install · remove · check (Python 3.11+, stdlib only)
catalog.toml             the vendored SDK's version, sha256 and origin
sdk/regulus.py           the official Regulus Python SDK 0.2.1, unchanged (see NOTICE)
modules/<id>/            one folder per module: module.toml, run.py, README.md, tests/
template/                a starter module to copy
tests/                   tests of catalog.py, and the real-core check (REGULUS_BIN)
```

## License

Apache-2.0, see [LICENSE](LICENSE). `sdk/regulus.py` is the Regulus Python SDK, also
Apache-2.0, copied from the Regulus repository; see [NOTICE](NOTICE).
