#!/usr/bin/env python3
"""regulus-modules: list, show, install and remove the modules of this catalogue.

Python 3.11 or newer, standard library only. It works on this clone and on a project
folder: it copies files and adds or removes one marked block of the project's
`regulus.toml`. It never runs module code, never uses the network and never trusts a
project.

    python3 catalog.py list
    python3 catalog.py show text-extract
    python3 catalog.py install text-extract --project ~/my-project
    python3 catalog.py remove text-extract --project ~/my-project
    python3 catalog.py check            # for contributors and CI

An installed module is a Regulus local module laid out like `regulus node init` lays one
out, with a `[[modules.local]]` entry: a Python one is `tools/<id>/run.py` with the Python
SDK beside it; a Rust one (`lang = "rust"` in its module.toml) is a crate in `tools/<id>/`
with the Rust helper as `src/regulus.rs`, which `regulus node build` compiles. Regulus
starts it only once the project is trusted (`regulus project trust <folder>`).
"""
import sys

if sys.version_info < (3, 11):
    sys.exit("catalog.py needs Python 3.11 or newer (it reads TOML with tomllib)")

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import tomllib  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent
CATALOG_FILE = "catalog.toml"
MODULES_DIR = "modules"
TEMPLATE_DIR = "template"
MODULE_FILE = "module.toml"
README = "README.md"
SDK_DIR = "sdk"
# The project side, as Regulus and `regulus node init` lay it out.
PROJECT_FILE = "regulus.toml"
TOOLS_DIR = "tools"
RUN_FILE = "run.py"
SDK_FILE = "regulus.py"
PYTHON = "python3"
# A module's language: what its files are and how the project runs it.
LANG_PYTHON = "python"
LANG_RUST = "rust"
LANGS = (LANG_PYTHON, LANG_RUST)
# A Rust module is a crate, as `regulus node init --lang rust` writes one: the helper that
# speaks the protocol is added as src/regulus.rs, `regulus node build` compiles the crate
# into target/, and the program is named after the module.
CARGO_FILE = "Cargo.toml"
MANIFEST_FILE = "regulus-module.toml"
RUST_MAIN = "src/main.rs"
RUST_SDK_FILE = "regulus.rs"
RUST_SDK_PATH = "src/regulus.rs"
BUILD_DIR = "target"
RUST_PROGRAM = "node-{id}"
# The files a module of each language must list.
REQUIRED_FILES = {LANG_PYTHON: (RUN_FILE,), LANG_RUST: (CARGO_FILE, MANIFEST_FILE, RUST_MAIN)}
# The file to read before trusting a project with the module.
MAIN_FILE = {LANG_PYTHON: RUN_FILE, LANG_RUST: RUST_MAIN}
# The one hidden file a crate may bring (it keeps target/ out of the project's git).
GITIGNORE = ".gitignore"
# How many folders deep a file of a crate may be.
MAX_FILE_DEPTH = 3
# What this tool leaves in an installed module's folder, to know the module later.
RECEIPT = ".regulus-modules.json"
CACHE_DIR = "__pycache__"
BEGIN = "# >>> regulus-modules: {id} >>>"
END = "# <<< regulus-modules: {id} <<<"
ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
SHA256 = re.compile(r"[0-9a-f]{64}")
# A file of a module: a name in its folder, never a path and never hidden.
PLAIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
BYTECODE = re.compile(r"(?P<stem>[A-Za-z0-9_]+)\.[A-Za-z0-9_-]+(?:\.opt-[12])?\.pyc")
SDK_VERSION = re.compile(rb'^__version__ = "([^"]+)"$', re.MULTILINE)


class Refused(Exception):
    """Nothing was changed; the message says why and what to do."""


def bullets(lines):
    return "\n".join(f"  - {line}" for line in lines)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def regular_file(path, what):
    """The bytes of `path`, which must be a regular file: not a link, not a folder."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise Refused(f"{what} is missing: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise Refused(f"{what} must be a regular file, not a link or a folder: {path}")
    return path.read_bytes()


def parse_toml(text, what):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise Refused(f"{what} is not valid TOML: {error}") from None


def read_toml(path, what):
    try:
        text = regular_file(path, what).decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refused(f"{what} is not UTF-8: {error}") from None
    return parse_toml(text, what)


def real_dir(path):
    return not path.is_symlink() and path.is_dir()


# --- the catalogue ----------------------------------------------------------------------


@dataclass
class Sdk:
    data: bytes
    version: str
    sha256: str
    origin: str
    path: str  # where it goes in an installed module
    name: str  # what it is, for people


@dataclass
class Module:
    id: str
    name: str
    version: str
    summary: str
    license: str
    tested_with: str
    files: dict  # file path -> sha256, in the order of module.toml
    folder: Path
    lang: str = LANG_PYTHON


def load_sdk(root=ROOT, lang=LANG_PYTHON):
    """The one copy of the Regulus SDK of `lang`, checked against `catalog.toml`: the
    Python SDK against [sdk], the Rust helper against [sdk.rust]."""
    table = read_toml(root / CATALOG_FILE, CATALOG_FILE).get("sdk")
    if not isinstance(table, dict):
        raise Refused(f"{CATALOG_FILE} has no [sdk] table")
    file_name, said_in = SDK_FILE, "[sdk]"
    if lang == LANG_RUST:
        file_name, said_in, table = RUST_SDK_FILE, "[sdk.rust]", table.get("rust")
        if not isinstance(table, dict):
            raise Refused(f"{CATALOG_FILE} has no [sdk.rust] table")
    if not real_dir(root / SDK_DIR):
        raise Refused(f"{SDK_DIR}/ must be a folder of this clone, not a link")
    data = regular_file(root / SDK_DIR / file_name, f"{SDK_DIR}/{file_name}")
    want, got = table.get("sha256"), sha256(data)
    if got != want:
        raise Refused(f"{SDK_DIR}/{file_name}: sha256 is {got}, {CATALOG_FILE} {said_in} "
                      f"says {want}")
    version = table.get("version")
    if not isinstance(version, str) or not version:
        raise Refused(f"{CATALOG_FILE} {said_in} has no version")
    origin = str(table.get("origin", ""))
    if lang == LANG_RUST:  # the helper carries no version: it is the one of that release
        return Sdk(data, version, got, origin, RUST_SDK_PATH, "Regulus Rust helper")
    said = SDK_VERSION.search(data)
    if not said or said.group(1).decode() != version:
        raise Refused(f"{SDK_DIR}/{SDK_FILE} is not version {version}, as {CATALOG_FILE} says")
    return Sdk(data, version, got, origin, SDK_FILE, "Regulus Python SDK")


def file_path_problem(file_name, lang):
    """Why `file_name` cannot be a file of a module of `lang`, or None if it can."""
    if not isinstance(file_name, str):
        return "must be a string"
    if lang == LANG_PYTHON:
        if not PLAIN_NAME.fullmatch(file_name) or file_name in (SDK_FILE, CACHE_DIR):
            return (f"must be a file name in the module's folder: no `/`, no leading `.`, "
                    f"not {SDK_FILE} (the SDK is added)")
        return None
    parts = file_name.split("/")
    plain = all(PLAIN_NAME.fullmatch(part) for part in parts[:-1]) and (
        PLAIN_NAME.fullmatch(parts[-1]) or parts[-1] == GITIGNORE)
    if not plain or len(parts) > MAX_FILE_DEPTH + 1:
        return (f"must be a path inside the module's folder, at most {MAX_FILE_DEPTH} folders "
                f"deep: plain names, no `..`, nothing hidden but {GITIGNORE}")
    if file_name == RUST_SDK_PATH or parts[0] == BUILD_DIR:
        return f"must not be {RUST_SDK_PATH} (the helper is added) or in {BUILD_DIR}/ (the build)"
    return None


def folders_of(file_names):
    """The folders that hold `file_names`, each as a relative path, outermost first."""
    found = set()
    for file_name in file_names:
        parts = file_name.split("/")[:-1]
        found.update("/".join(parts[:depth]) for depth in range(1, len(parts) + 1))
    return sorted(found, key=lambda folder: (folder.count("/"), folder))


def module_ids(root=ROOT):
    base = root / MODULES_DIR
    if not real_dir(base):
        return []
    return sorted(p.name for p in base.iterdir() if real_dir(p))


def load_module(folder, expect_id=None):
    """The module described by `folder/module.toml`. With `expect_id`, its id must be the
    folder's name (every module in `modules/`)."""
    if not real_dir(folder):
        raise Refused(f"no module `{expect_id or folder.name}` in this catalogue "
                      "(see: python3 catalog.py list)")
    data = read_toml(folder / MODULE_FILE, f"{folder.name}/{MODULE_FILE}")
    problems = []

    def text(key):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            problems.append(f"`{key}` must be a one-line, non-empty string")
            return ""
        return value

    module_id, version = text("id"), text("version")
    name, summary, license_id, tested_with = (
        text("name"), text("summary"), text("license"), text("tested_with"))
    if module_id and not ID.fullmatch(module_id):
        problems.append(f"`id` {module_id!r} must be 1 to 64 of a-z, 0-9, `-`, `_`, "
                        "starting with a letter or a digit")
    if expect_id is not None and module_id and module_id != expect_id:
        problems.append(f"`id` is `{module_id}` but the folder is `{expect_id}`")
    if version and not VERSION.fullmatch(version):
        problems.append(f"`version` {version!r} must look like 1.2.3")
    lang = data.get("lang", LANG_PYTHON)
    if lang not in LANGS:
        problems.append(f"`lang` {lang!r} must be one of {', '.join(LANGS)}")
        lang = LANG_PYTHON
    files = {}
    entries = data.get("file")
    if not isinstance(entries, list) or not entries:
        problems.append("it needs at least one [[file]]")
        entries = []
    for entry in entries:
        file_name = entry.get("path") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        problem = file_path_problem(file_name, lang)
        if problem:
            problems.append(f"[[file]] path {file_name!r} {problem}")
        elif file_name in files:
            problems.append(f"[[file]] {file_name} is listed twice")
        elif not isinstance(digest, str) or not SHA256.fullmatch(digest):
            problems.append(f"[[file]] {file_name}: sha256 must be 64 lowercase hex digits")
        else:
            files[file_name] = digest
    for required in REQUIRED_FILES[lang]:
        if entries and required not in files:
            problems.append(f"[[file]] must list {required}: a {lang} module needs it")
    if problems:
        raise Refused(f"{folder.name}/{MODULE_FILE}:\n{bullets(problems)}")
    return Module(module_id, name, version, summary, license_id, tested_with, files, folder,
                  lang)


def find_module(module_id, root=ROOT):
    if not ID.fullmatch(module_id):
        raise Refused(f"`{module_id}` is not a module id (see: python3 catalog.py list)")
    return load_module(root / MODULES_DIR / module_id, module_id)


def module_payload(module):
    """The bytes of the module's files, each checked against its sha256."""
    payload, problems = {}, []
    for file_name, want in module.files.items():
        where = f"{module.folder.name}/{file_name}"
        try:
            data = regular_file(module.folder / file_name, where)
        except Refused as error:
            problems.append(str(error))
            continue
        got = sha256(data)
        if got != want:
            problems.append(f"{where}: sha256 is {got}, {MODULE_FILE} says {want}")
        payload[file_name] = data
    if problems:
        raise Refused(f"the files of `{module.id}` do not match {MODULE_FILE}:\n"
                      f"{bullets(problems)}")
    return payload


def inspect_nodes(source, root=ROOT):
    """What the SDK reads of a `run.py` without running it (`regulus.inspect`)."""
    spec = importlib.util.spec_from_file_location("regulus_catalog_sdk", root / SDK_DIR / SDK_FILE)
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    return sdk.inspect(source)


def rust_program(module_id):
    """The Cargo package, and so the program, of a Rust module: what `regulus node build`
    expects to have built."""
    return RUST_PROGRAM.format(id=module_id)


def rust_crate(module, payload):
    """The node types a Rust module declares in its `regulus-module.toml`, and what in the
    crate would not build into the program Regulus runs (a list of problems)."""
    where = f"{module.folder.name}/"
    problems = []

    def table_of(file_name):
        try:
            return parse_toml(payload[file_name].decode("utf-8"), where + file_name)
        except (Refused, UnicodeDecodeError) as error:
            problems.append(str(error))
            return {}

    cargo, manifest = table_of(CARGO_FILE), table_of(MANIFEST_FILE)
    package = cargo.get("package") if isinstance(cargo.get("package"), dict) else {}
    if package.get("name") != rust_program(module.id):
        problems.append(f"{where}{CARGO_FILE}: the package must be named "
                        f"`{rust_program(module.id)}`, the program `regulus node build` expects")
    if package.get("version") != module.version:
        problems.append(f"{where}{CARGO_FILE}: the package's version must be {module.version}, "
                        f"as {MODULE_FILE} says")
    if "workspace" not in cargo:
        problems.append(f"{where}{CARGO_FILE}: it needs an empty [workspace] table, so a Cargo "
                        "workspace above the project does not adopt the crate")
    declared = manifest.get("module") if isinstance(manifest.get("module"), dict) else {}
    said = declared.get("id")
    if isinstance(said, str) and "@" in said and said.rsplit("@", 1)[1] != module.version:
        problems.append(f"{where}{MANIFEST_FILE}: the module id says version "
                        f"{said.rsplit('@', 1)[1]}, {MODULE_FILE} says {module.version}")
    nodes = manifest.get("node")
    good = isinstance(nodes, list) and nodes and all(
        isinstance(node, dict) and isinstance(node.get("type"), str)
        and all(isinstance(port, dict) and all(isinstance(port.get(key), str) for key in
                                               ("id", "capability", "direction"))
                for port in node.get("port", []))
        and all(isinstance(field, dict) and isinstance(field.get("id"), str)
                for field in node.get("field", []))
        for node in nodes)
    if not good:
        problems.append(f"{where}{MANIFEST_FILE}: it must declare at least one [[node]] with a "
                        "type, and ports with id, capability and direction")
        nodes = []
    return nodes, problems


def node_types(module, payload, root=ROOT):
    """The node types of a module, read without running or building it."""
    if module.lang == LANG_RUST:
        return rust_crate(module, payload)[0]
    return [found["node"] for found in inspect_nodes(payload[RUN_FILE], root)["nodes"]]


# --- the project ------------------------------------------------------------------------


@dataclass
class Project:
    root: Path
    file: Path
    text: str
    table: dict
    id: str


def open_project(folder):
    try:
        root = Path(folder).expanduser().resolve(strict=True)
    except OSError:
        raise Refused(f"no such folder: {folder}") from None
    if not root.is_dir():
        raise Refused(f"{folder} is not a folder")
    file = root / PROJECT_FILE
    if not os.path.lexists(file):
        raise Refused(f"{root} is not a Regulus project: it has no {PROJECT_FILE}. Make it "
                      f"one with `regulus project open {root}`, then try again")
    try:
        text = regular_file(file, PROJECT_FILE).decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refused(f"{PROJECT_FILE} is not UTF-8: {error}") from None
    table = parse_toml(text, PROJECT_FILE)
    project = table.get("project")
    named = project.get("id") if isinstance(project, dict) else None
    return Project(root, file, text, table, named if isinstance(named, str) else root.name)


def local_entries(table):
    """The `[[modules.local]]` entries, refusing a file whose shape the core would not read."""
    modules = table.get("modules", {})
    if not isinstance(modules, dict):
        raise Refused(f"{PROJECT_FILE}: `modules` must be a table; fix it first")
    local = modules.get("local", [])
    if not isinstance(local, list) or not all(isinstance(e, dict) for e in local):
        raise Refused(f"{PROJECT_FILE}: `modules.local` must be a list of tables; fix it first")
    return local


def entry_of(module):
    if module.lang == LANG_RUST:  # where `regulus node build` leaves the program
        command = [f"{TOOLS_DIR}/{module.id}/{BUILD_DIR}/release/{rust_program(module.id)}"]
    else:
        command = [PYTHON, f"{TOOLS_DIR}/{module.id}/{RUN_FILE}"]
    return {"id": module.id, "command": command, "version": module.version}


def block_of(module):
    """The text added to `regulus.toml`: the entry between two marker comments."""
    entry = entry_of(module)
    quote = json.dumps  # a JSON string of these characters is a TOML basic string
    return "\n".join([
        BEGIN.format(id=module.id),
        f"# {module.name} {module.version}, from the regulus-modules catalogue. It runs only "
        "once the project",
        "# is trusted. Remove it with: python3 catalog.py remove "
        f"{module.id} --project <this folder>",
        "[[modules.local]]",
        f"id = {quote(entry['id'])}",
        f"command = [{', '.join(quote(part) for part in entry['command'])}]",
        f"version = {quote(entry['version'])}",
        END.format(id=module.id),
    ]) + "\n"


def separator(text):
    """What goes between the file and the block: a blank line, as `regulus node init` does."""
    if not text or text.endswith("\n\n"):
        return ""
    return "\n" if text.endswith("\n") else "\n\n"


def normalized(table):
    """`table` without an empty `modules.local` or `modules` (what removing the last entry
    leaves: TOML cannot say "an empty list of tables")."""
    table = copy.deepcopy(table)
    modules = table.get("modules")
    if isinstance(modules, dict):
        if modules.get("local") == []:
            del modules["local"]
        if not modules:
            del table["modules"]
    return table


def write_new(path, data):
    """Writes `data` to `path`, which must not exist (never through a link)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o666)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def replace_text(path, before, after):
    """Replaces `path` with `after` through a new file beside it and one rename, keeping its
    permissions, only if it still holds `before`; else nothing is written."""
    if regular_file(path, PROJECT_FILE) != before.encode("utf-8"):
        raise Refused(f"{PROJECT_FILE} changed while this ran; nothing was changed, try again")
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write_new(temp, after.encode("utf-8"))
        os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp, path)
    except BaseException:
        if os.path.lexists(temp):
            temp.unlink()
        raise


def undo(created):
    """Removes what an install created, newest first."""
    for path in reversed(created):
        try:
            if real_dir(path):
                path.rmdir()
            else:
                path.unlink()
        except OSError:
            pass


# --- install and remove -----------------------------------------------------------------


@dataclass
class Receipt:
    module: str
    version: str
    files: dict  # file path -> sha256, the SDK included
    added: str  # the exact text added to regulus.toml
    entry: dict
    created_tools: bool
    lang: str = LANG_PYTHON

    @property
    def block(self):
        return self.added.lstrip("\n")


def install(module_id, folder, root=ROOT):
    """Copies the module into `tools/<id>/` of the project and appends its entry to
    `regulus.toml`. Every check runs before the first write; files are created new, never
    over something; `regulus.toml` is replaced last, in one rename, only if it did not
    change; if anything fails, what was created is removed."""
    module = find_module(module_id, root)
    payload = module_payload(module)
    sdk = load_sdk(root, module.lang)
    payload[sdk.path] = sdk.data
    project = open_project(folder)
    if any(entry.get("id") == module.id for entry in local_entries(project.table)):
        raise Refused(f"{PROJECT_FILE} already has a [[modules.local]] with id `{module.id}`; "
                      "nothing was installed")
    if BEGIN.format(id=module.id) in project.text:
        raise Refused(f"{PROJECT_FILE} already has a regulus-modules block for `{module.id}`; "
                      "nothing was installed")
    tools = project.root / TOOLS_DIR
    if os.path.lexists(tools) and not real_dir(tools):
        raise Refused(f"{TOOLS_DIR} in the project must be a folder, not a link or a file")
    target = tools / module.id
    if os.path.lexists(target):
        raise Refused(f"{TOOLS_DIR}/{module.id} already exists in the project; "
                      "nothing was installed")
    block = block_of(module)
    added = separator(project.text) + block
    after = project.text + added
    expected = copy.deepcopy(project.table)
    expected.setdefault("modules", {}).setdefault("local", []).append(entry_of(module))
    try:
        same = tomllib.loads(after) == expected
    except tomllib.TOMLDecodeError:
        same = False
    if not same:
        raise Refused(f"appending the entry would change what {PROJECT_FILE} says (is "
                      "`modules.local` written inline?); nothing was installed. Add it by "
                      f"hand:\n\n{block}")
    receipt = {
        "catalog": "regulus-modules",
        "module": module.id,
        "version": module.version,
        "lang": module.lang,
        "files": {name: sha256(data) for name, data in payload.items()},
        "sdk": {"version": sdk.version, "origin": sdk.origin},
        "regulus_toml": {"added": added, "entry": entry_of(module)},
        "created_tools": not os.path.lexists(tools),
    }
    created = []
    try:
        if receipt["created_tools"]:
            os.mkdir(tools)
            created.append(tools)
        os.mkdir(target)
        created.append(target)
        for inner in folders_of(payload):
            os.mkdir(target / inner)
            created.append(target / inner)
        for file_name, data in payload.items():
            created.append(target / file_name)
            write_new(target / file_name, data)
        created.append(target / RECEIPT)
        write_new(target / RECEIPT, (json.dumps(receipt, indent=2) + "\n").encode())
        replace_text(project.file, project.text, after)
    except BaseException:
        undo(created)
        raise
    return module, project, [*payload, RECEIPT]


def read_receipt(folder, module_id):
    """What install recorded in `folder`, validated: names stay inside the folder."""
    path = folder / RECEIPT
    if not os.path.lexists(path):
        raise Refused(f"{TOOLS_DIR}/{module_id} has no {RECEIPT}: catalog.py did not install "
                      "it, so it is yours to remove by hand; nothing was removed")
    try:
        raw = json.loads(regular_file(path, RECEIPT))
        files, toml = raw["files"], raw["regulus_toml"]
        receipt = Receipt(raw["module"], raw["version"], dict(files), toml["added"],
                          toml["entry"], raw["created_tools"], raw.get("lang", LANG_PYTHON))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Refused(f"{TOOLS_DIR}/{module_id}/{RECEIPT} is not what catalog.py writes; "
                      "nothing was removed") from None
    good = (
        receipt.module == module_id
        and receipt.lang in LANGS
        and all(isinstance(n, str) and isinstance(d, str) and SHA256.fullmatch(d)
                and (n == (RUST_SDK_PATH if receipt.lang == LANG_RUST else SDK_FILE)
                     or file_path_problem(n, receipt.lang) is None)
                for n, d in receipt.files.items())
        and isinstance(receipt.added, str)
        and receipt.block.startswith(BEGIN.format(id=module_id))
        and isinstance(receipt.entry, dict) and receipt.entry.get("id") == module_id
        and isinstance(receipt.created_tools, bool)
    )
    if not good:
        raise Refused(f"{TOOLS_DIR}/{module_id}/{RECEIPT} does not describe `{module_id}`; "
                      "nothing was removed")
    return receipt


def only_bytecode(folder, stems):
    """Whether `folder` is a `__pycache__` holding only the compiled copies of `stems`."""
    if not real_dir(folder):
        return False
    with os.scandir(folder) as entries:
        for entry in entries:
            match = BYTECODE.fullmatch(entry.name)
            if not entry.is_file(follow_symlinks=False) or not match \
                    or match.group("stem") not in stems:
                return False
    return True


def remove(module_id, folder):
    """Removes a module this tool installed: its files and its block of `regulus.toml`, only
    if none of them changed and the folder holds nothing else (but Python's bytecode cache,
    or the `target/` a Rust module was built into, which go with it). Otherwise refuses and
    changes nothing."""
    if not ID.fullmatch(module_id):
        raise Refused(f"`{module_id}` is not a module id")
    project = open_project(folder)
    tools = project.root / TOOLS_DIR
    target = tools / module_id
    if not real_dir(tools) or not real_dir(target):
        raise Refused(f"{project.root} has no folder {TOOLS_DIR}/{module_id}; nothing was removed")
    receipt = read_receipt(target, module_id)
    problems, present, derived = [], [], set()
    stems = {name[:-3] for name in receipt.files if name.endswith(".py")}
    folders = folders_of(receipt.files)

    def look(folder, prefix):
        for entry in sorted(os.scandir(folder), key=lambda e: e.name):
            name = prefix + entry.name
            shown = f"{TOOLS_DIR}/{module_id}/{name}"
            if name == RECEIPT:
                continue
            if name in receipt.files:
                if not entry.is_file(follow_symlinks=False):
                    problems.append(f"{shown} is no longer a regular file")
                elif sha256(Path(entry.path).read_bytes()) != receipt.files[name]:
                    problems.append(f"{shown} changed since it was installed")
                else:
                    present.append(name)
            elif name in folders and entry.is_dir(follow_symlinks=False):
                look(entry.path, name + "/")
            elif receipt.lang == LANG_PYTHON and name == CACHE_DIR \
                    and only_bytecode(Path(entry.path), stems):
                derived.add(CACHE_DIR)
            elif receipt.lang == LANG_RUST and name == BUILD_DIR \
                    and entry.is_dir(follow_symlinks=False):
                derived.add(BUILD_DIR)  # what `regulus node build` made of these files
            else:
                problems.append(f"{shown} was not installed by catalog.py")

    look(target, "")
    after = None
    begin = BEGIN.format(id=module_id)
    ours = [e for e in local_entries(project.table) if e.get("id") == module_id]
    if project.text.count(begin) == 0 and not ours:
        pass  # the entry was already taken out of regulus.toml: only the files go
    elif project.text.count(begin) != 1 or ours != [receipt.entry]:
        problems.append(f"the [[modules.local]] entry `{module_id}` in {PROJECT_FILE} was changed "
                        "or is there more than once")
    else:
        for text in (receipt.added, receipt.block):
            if project.text.count(text) == 1:
                after = project.text.replace(text, "", 1)
                break
        expected = copy.deepcopy(project.table)
        expected["modules"]["local"].remove(receipt.entry)
        if after is None or normalized(parse_toml(after, PROJECT_FILE)) != normalized(expected):
            after = None
            problems.append(f"the regulus-modules block for `{module_id}` in {PROJECT_FILE} "
                            "was changed")
    if problems:
        raise Refused(
            f"nothing was removed:\n{bullets(problems)}\nTo keep your changes, copy them out of "
            f"{TOOLS_DIR}/{module_id}/ first. Then delete that folder and the `{begin}` block "
            f"of {PROJECT_FILE} by hand, or put the installed files back and run remove again.")
    aside = tools / f".{module_id}.removing-{os.getpid()}"
    if os.path.lexists(aside):
        raise Refused(f"{aside} is in the way; nothing was removed")
    os.rename(target, aside)
    try:
        if after is not None:
            replace_text(project.file, project.text, after)
    except BaseException:
        os.rename(aside, target)
        raise
    for file_name in [*present, RECEIPT]:
        (aside / file_name).unlink()
    if CACHE_DIR in derived:
        for entry in os.scandir(aside / CACHE_DIR):
            if BYTECODE.fullmatch(entry.name):
                os.unlink(entry.path)
    if BUILD_DIR in derived:
        shutil.rmtree(aside / BUILD_DIR)
    kept = []
    for path in (aside / CACHE_DIR, *(aside / inner for inner in reversed(folders)), aside):
        try:
            if os.path.lexists(path):
                path.rmdir()
        except OSError:
            kept.append(path)
    if kept:  # something new appeared meanwhile: leave it where the user put it
        os.rename(aside, target)
    elif receipt.created_tools and not any(tools.iterdir()):
        tools.rmdir()
    return receipt, project, after is not None, bool(kept)


# --- the commands -----------------------------------------------------------------------


def command_list(_args):
    for module_id in module_ids():
        try:
            module = find_module(module_id)
            print(f"{module.id:<20} {module.version:<9} {module.summary}")
        except Refused as error:
            print(f"{module_id:<20} broken    {error}")


def describe_port(port):
    return f"{port['id']} {port['capability']}"


def command_show(args):
    module = find_module(args.module)
    payload = module_payload(module)
    sdk = load_sdk(lang=module.lang)
    kind = (f"Rust local module (helper of Regulus {sdk.version}), built with "
            "`regulus node build`," if module.lang == LANG_RUST
            else f"Python local module (SDK {sdk.version}),")
    print(f"{module.id} {module.version} - {module.name}")
    print(module.summary)
    print(f"license {module.license} · tested with Regulus {module.tested_with} · "
          f"{kind} runs only in a trusted project")
    print("\nnode types (module_id \"<project>/" + module.id + "\"):")
    for node in node_types(module, payload):
        ports = node.get("port", [])
        print(f"  {node['type']}  ({node.get('name', '')})")
        print("    in:     " + ", ".join(describe_port(p) for p in ports
                                         if p["direction"] == "input"))
        print("    out:    " + ", ".join(describe_port(p) for p in ports
                                         if p["direction"] == "output"))
        for field in node.get("field", []):
            options = (f", one of {', '.join(map(str, field['options']))}"
                       if field.get("options") else "")
            print(f"    field:  {field['id']} ({field.get('type', 'text')}, default "
                  f"{json.dumps(field.get('default'))}{options})")
    print("\nfiles installed into tools/" + module.id + "/ (sha256):")
    width = max(map(len, [*module.files, sdk.path]))
    for file_name, digest in module.files.items():
        print(f"  {file_name:<{width}} {digest}")
    print(f"  {sdk.path:<{width}} {sdk.sha256}  ({sdk.name} {sdk.version})")
    print(f"\ninstall: python3 catalog.py install {module.id} --project <project folder>")
    print(f"docs:    {MODULES_DIR}/{module.id}/{README}")


def command_install(args):
    module, project, files = install(args.module, args.project)
    print(f"installed {module.id} {module.version} into {project.root}")
    for file_name in files:
        print(f"  created {TOOLS_DIR}/{module.id}/{file_name}")
    print(f"  added its [[modules.local]] entry to {PROJECT_FILE} "
          f"(module id {project.id}/{module.id})")
    build = ""
    if module.lang == LANG_RUST:
        build = (f"  regulus node build {project.root} --id {module.id}"
                 "   # cargo build --release: needs cargo, and the network the first time\n")
    print(f"\nIt does not run yet: Regulus starts a project's local modules only once the project "
          f"is trusted,\nwith your user's permissions (no sandbox). Read "
          f"{TOOLS_DIR}/{module.id}/{MAIN_FILE[module.lang]}, then:\n"
          f"  regulus project trust {project.root}\n{build}"
          f"  regulus project reload {project.id} --module {module.id}"
          f"   # if the core has it open, else: regulus project open {project.root}")
    print(f"Its node types: python3 catalog.py show {module.id}")


def command_remove(args):
    receipt, project, edited, kept = remove(args.module, args.project)
    print(f"removed {receipt.module} {receipt.version} from {project.root}")
    if kept:
        print(f"  kept {TOOLS_DIR}/{receipt.module}/: a file not installed by catalog.py "
              "appeared in it while removing")
    else:
        print(f"  deleted {TOOLS_DIR}/{receipt.module}/")
    if edited:
        print(f"  removed its [[modules.local]] entry from {PROJECT_FILE}")
    print(f"If the core has the project open: regulus project reload {project.id}")


def command_check(_args, root=ROOT):
    problems = []
    sdks = {}
    for lang in LANGS:
        try:
            sdks[lang] = load_sdk(root, lang)
        except Refused as error:
            problems.append(str(error))
    readme = (root / README).read_text("utf-8") if (root / README).is_file() else ""
    ids = module_ids(root)
    if not ids:
        problems.append(f"{MODULES_DIR}/ has no modules")
    folders = [(root / MODULES_DIR / i, i) for i in ids] + [(root / TEMPLATE_DIR, None)]
    for folder, expect in folders:
        where = f"{folder.relative_to(root)}"
        try:
            module = load_module(folder, expect)
            payload = module_payload(module)
        except Refused as error:
            problems.append(f"{where}: {error}")
            continue
        if module.lang == LANG_RUST:
            problems.extend(rust_crate(module, payload)[1])
            # In this clone the crate reaches the one copy of the helper through a link, so
            # `cargo test` works here; install writes the real file in its place.
            helper, sdk = folder / RUST_SDK_PATH, sdks.get(LANG_RUST)
            if sdk and not (helper.is_file() and sha256(helper.read_bytes()) == sdk.sha256):
                problems.append(f"{where}/{RUST_SDK_PATH} must be a link to "
                                f"{SDK_DIR}/{RUST_SDK_FILE}, the one copy of the helper")
        else:
            read = inspect_nodes(payload[RUN_FILE], root)
            errors = [d["message"] for d in read["diagnostics"] if d["severity"] == "error"]
            if errors or not read["nodes"] or not read["complete"]:
                problems.append(
                    f"{where}/{RUN_FILE}: the SDK cannot read its node types without running "
                    f"it (use literal arguments): {errors or read['diagnostics']}")
        if not (folder / README).is_file():
            problems.append(f"{where}: has no {README}")
        row = any(f"`{module.id}`" in line and module.version in line
                  for line in readme.splitlines())
        if expect is not None and not row:
            problems.append(f"{README} has no catalogue row for `{module.id}` {module.version}")
    if problems:
        raise Refused(f"check failed:\n{bullets(problems)}")
    print(f"ok: {len(ids)} module(s) and the template match their manifests; "
          f"{SDK_DIR}/{SDK_FILE} and {SDK_DIR}/{RUST_SDK_FILE} match {CATALOG_FILE}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="catalog.py",
        description="List, show, install and remove the Regulus modules of this catalogue. "
                    "Runs no module code and never trusts a project.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("list", help="the modules of this catalogue").set_defaults(
        run=command_list)
    show = commands.add_parser("show", help="a module's nodes, files and checksums")
    show.add_argument("module")
    show.set_defaults(run=command_show)
    for name, run, help_text in (
            ("install", command_install, "copy a module into a project (it does not trust it)"),
            ("remove", command_remove, "remove a module this tool installed, if unchanged")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("module")
        sub.add_argument("--project", required=True, metavar="FOLDER",
                         help="the Regulus project folder (the one with regulus.toml)")
        sub.set_defaults(run=run)
    commands.add_parser("check", help="verify manifests, checksums and the README "
                                      "(contributors, CI)").set_defaults(run=command_check)
    args = parser.parse_args(argv)
    try:
        args.run(args)
    except Refused as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
