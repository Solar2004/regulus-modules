# Contributing a module

A module here is one Regulus local module written in Python with the official SDK, that
anyone can install with `python3 catalog.py install <id> --project <folder>`. Adding one
is a pull request with one new folder and one new README row.

What Regulus expects of a local module (the protocol, trust, reload, ports that depend
on the config) is in the Regulus docs:
[`docs/18-custom-nodes.md`](https://github.com/Solar2004/Regulus/blob/master/docs/18-custom-nodes.md)
and [`docs/03-modules.md` §6](https://github.com/Solar2004/Regulus/blob/master/docs/03-modules.md).
The SDK reference is the docstring at the top of [`sdk/regulus.py`](sdk/regulus.py).

## 1. Start from the template

```sh
git clone https://github.com/<you>/regulus-modules && cd regulus-modules
cp -r template modules/my-tool
mv modules/my-tool/tests/test_my_module.py modules/my-tool/tests/test_my_tool.py
```

The id (`my-tool` here) is 1 to 64 characters of `a-z`, `0-9`, `-`, `_`, the folder's name,
and the id of the `[[modules.local]]` entry it gets in a project. In
`modules/my-tool/module.toml` set `id`, `name`, `version` (start at `0.1.0`), a one-line
`summary`, `license` (Apache-2.0 unless you say otherwise) and `tested_with`, the Regulus
version you ran it with.

## 2. Write the nodes

`run.py` declares node types with `@rg.node(...)` and sends values with `ctx.emit`:

```python
import regulus as rg

@rg.node("shout", name="Shout",
         inputs=[rg.then_in("in", "text@1")],
         outputs=[rg.then_out("out", "text@1")],
         fields=[rg.field("marks", "text", default="!", label="Marks")])
def shout(ctx, port, value):
    if not isinstance(value, str):
        raise rg.InvalidInput("text@1 must be a string")
    ctx.emit("out", value.upper() + ctx.config["marks"])

if __name__ == "__main__":
    rg.run(name="My tool")
```

- **Standard library only.** There is no dependency installer: what `run.py` imports must
  ship in the module's folder or come with Python (3.9 or newer).
- **Literal arguments in `@rg.node(...)`** (strings, lists, numbers, or module-level names
  bound once to a literal). Regulus reads the node types from the source without running
  it (`source.check`), and so do `catalog.py show` and `catalog.py check`.
- **No version in `rg.run()`.** The version comes from `module.toml`; install writes it
  into the project's entry.
- **Use the SDK as it is.** Do not copy `regulus.py` into your folder: install puts the
  catalogue's single copy beside `run.py`.
- **Say what it touches.** A module runs with the user's permissions once they trust the
  project. If it reads or writes files, runs commands or uses the network, the README
  says so, and why.
- Keep one module to one purpose. A few node types that belong together are fine.

Extra Python files go in the same folder (no subfolders) and are imported as usual:
Python puts `run.py`'s folder on the import path.

## 3. Test it

```sh
python3 -m unittest discover -s modules/my-tool/tests -v
```

Test what each node sends for real inputs, and what it refuses. The template's test shows
how to call a handler with a small fake context; `modules/text-extract/tests` also drives
the module over stdin/stdout the way the core does.

Then try it in a real project (a throwaway one is best):

```sh
regulus serve                                           # in another terminal, left running
mkdir -p /tmp/try && regulus project open /tmp/try      # writes regulus.toml; id: try
python3 catalog.py install my-tool --project /tmp/try   # after step 4
regulus project trust /tmp/try
regulus project reload try --module my-tool
regulus module show try/my-tool
```

`install` needs an existing project (a folder with a `regulus.toml`) and the `regulus
project` commands need the running core; the [README](README.md#use-a-module) explains
both. In other projects, `<project-id>` is the first column of `regulus project ls`.

## 4. List its files and checksums

Every file installed into a project is listed in `module.toml` with its sha256:

```toml
[[file]]
path = "run.py"
sha256 = "…"
```

Run the check; when a file changed it prints the sha256 to write:

```sh
python3 catalog.py check
```

## 5. Add the README row and open a pull request

Write `modules/my-tool/README.md` from the template's (what it is for in a workflow, a
table of node types, an example, limits), and add a row to the catalogue table of the
top-level [README.md](README.md) with the same id and version (`check` looks for it).

Commit with a [Conventional Commits](https://www.conventionalcommits.org/) message such
as `feat(modules): add my-tool`, and open a pull request. CI runs `catalog.py check` and
every test folder on Python 3.11 and the latest Python 3.

## Changing a published module

Bump `version`, update the sha256 values and the README row, and say what changed in the
pull request. A user upgrades with `remove` then `install`; `remove` refuses if they edited
the installed files, so their edits are never lost silently.

## Keeping a module to yourself

You do not need this repository for that: `regulus node init <project> --id <id>`
writes a Python node straight into a project. A fork of this repository is also a fine
private catalogue: the same commands work on any clone.
