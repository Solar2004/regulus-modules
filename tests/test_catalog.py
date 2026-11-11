"""Tests of catalog.py: install and remove into a project folder, what they refuse, and
that nothing else in the project changes. No Regulus core is needed (see
test_real_core.py for that).

Run: python3 -m unittest discover -s tests
"""
import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import catalog  # noqa: E402

MODULE = "text-extract"
RUST_MODULE = "scrapling"

# A project file with what install and remove must keep: comments, a trailing comment,
# keys the core does not know, another local module and a table after it.
PROJECT = """\
# My project: these comments must survive.
[project]
name = "Demo"   # a trailing comment
flavour = "kept"

[modules]
disable = []

[[modules.local]]
# written by hand
id = "mine"
command = ["python3", "tools/mine/run.py"]
env = { A = "1" }

[client]
theme = "dark"
"""


def snapshot(root):
    """Every path under `root` with its bytes (files), target (links) or None (folders)."""
    seen = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(folder, name)
            key = str(path.relative_to(root))
            if path.is_symlink():
                seen[key] = ("link", os.readlink(path))
            elif path.is_dir():
                seen[key] = None
            else:
                seen[key] = path.read_bytes()
    return seen


class ProjectCase(unittest.TestCase):
    """A temporary project `demo` with PROJECT and a hand-made `tools/mine/run.py`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="regulus-modules-test-"))
        self.project = self.tmp / "demo"
        self.project.mkdir()
        self.write("regulus.toml", PROJECT)
        self.write("tools/mine/run.py", "print('mine')\n")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def write(self, relative, text):
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def toml(self):
        return (self.project / "regulus.toml").read_text()

    def install(self, root=catalog.ROOT):
        return catalog.install(MODULE, str(self.project), root)

    def remove(self):
        return catalog.remove(MODULE, str(self.project))

    def assert_refused(self, action, *words):
        """`action` is refused, says `words`, and the project is exactly as before."""
        before = snapshot(self.project)
        with self.assertRaises(catalog.Refused) as caught:
            action()
        for word in words:
            self.assertIn(word, str(caught.exception))
        self.assertEqual(snapshot(self.project), before, "a refusal changes nothing")
        return str(caught.exception)


class Install(ProjectCase):
    def test_it_adds_the_files_and_one_block_and_keeps_the_rest(self):
        os.chmod(self.project / "regulus.toml", 0o640)
        before = snapshot(self.project)
        self.install()
        text = self.toml()
        self.assertTrue(text.startswith(PROJECT), "the file is kept byte for byte, then added to")
        expected = tomllib.loads(PROJECT)
        expected["modules"]["local"].append({
            "id": MODULE, "command": ["python3", "tools/text-extract/run.py"],
            "version": "0.1.0"})
        self.assertEqual(tomllib.loads(text), expected)
        self.assertEqual(text.count("# >>> regulus-modules: text-extract >>>"), 1)
        self.assertEqual(stat.S_IMODE((self.project / "regulus.toml").stat().st_mode), 0o640)
        installed = self.project / "tools" / MODULE
        self.assertEqual((installed / "run.py").read_bytes(),
                         (ROOT / "modules" / MODULE / "run.py").read_bytes())
        self.assertEqual((installed / "regulus.py").read_bytes(),
                         (ROOT / "sdk" / "regulus.py").read_bytes())
        after = snapshot(self.project)
        new = sorted(set(after) - set(before))
        self.assertEqual(new, ["tools/text-extract", "tools/text-extract/.regulus-modules.json",
                               "tools/text-extract/regulus.py", "tools/text-extract/run.py"])
        changed = [k for k in before if after[k] != before[k]]
        self.assertEqual(changed, ["regulus.toml"], "nothing else is touched")

    def test_it_refuses_an_id_taken_a_folder_in_the_way_or_a_second_install(self):
        self.write("regulus.toml", PROJECT + '\n[[modules.local]]\nid = "text-extract"\n'
                                             'command = ["x"]\n')
        self.assert_refused(self.install, "already has a [[modules.local]] with id")
        self.write("regulus.toml", PROJECT + "# >>> regulus-modules: text-extract >>>\n")
        self.assert_refused(self.install, "already has a regulus-modules block")
        self.write("regulus.toml", PROJECT)
        self.write("tools/text-extract/notes.txt", "mine\n")
        self.assert_refused(self.install, "tools/text-extract already exists")
        shutil.rmtree(self.project / "tools" / MODULE)
        self.install()
        self.assert_refused(self.install, "already has a [[modules.local]] with id")

    def test_it_refuses_a_file_it_cannot_append_to(self):
        self.write("regulus.toml", '[modules]\nlocal = [{ id = "a", command = ["b"] }]\n')
        self.assert_refused(self.install, "would change what regulus.toml says")
        self.write("regulus.toml", "modules = { disable = [] }\n")
        self.assert_refused(self.install, "would change what regulus.toml says")
        self.write("regulus.toml", "[modules\n")
        self.assert_refused(self.install, "not valid TOML")
        self.write("regulus.toml", "modules = 3\n")
        self.assert_refused(self.install, "`modules` must be a table")
        real = self.write("elsewhere.toml", PROJECT)
        (self.project / "regulus.toml").unlink()
        (self.project / "regulus.toml").symlink_to(real)
        self.assert_refused(self.install, "must be a regular file")
        (self.project / "regulus.toml").unlink()
        self.assert_refused(self.install, "is not a Regulus project")

    def test_tools_must_be_a_real_folder(self):
        shutil.rmtree(self.project / "tools")
        (self.tmp / "outside").mkdir()
        (self.project / "tools").symlink_to(self.tmp / "outside")
        self.assert_refused(self.install, "must be a folder, not a link")
        self.assertEqual(list((self.tmp / "outside").iterdir()), [])

    def test_a_failed_install_removes_what_it_created(self):
        shutil.rmtree(self.project / "tools")
        before = snapshot(self.project)
        with mock.patch.object(catalog, "replace_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(snapshot(self.project), before, "no tools/ left behind")

    def test_regulus_toml_changed_meanwhile_is_not_overwritten(self):
        real = catalog.write_new

        def meddle(path, data):
            real(path, data)
            if path.name == catalog.RECEIPT:  # the last file before regulus.toml
                self.write("regulus.toml", PROJECT + "# edited meanwhile\n")

        with mock.patch.object(catalog, "write_new", side_effect=meddle):
            with self.assertRaises(catalog.Refused) as caught:
                self.install()
        self.assertIn("changed while this ran", str(caught.exception))
        self.assertEqual(self.toml(), PROJECT + "# edited meanwhile\n")
        self.assertFalse((self.project / "tools" / MODULE).exists())


class Remove(ProjectCase):
    def test_install_then_remove_leaves_the_project_exactly_as_it_was(self):
        for tools_before in (True, False):
            with self.subTest(tools_existed=tools_before):
                if not tools_before:
                    shutil.rmtree(self.project / "tools")
                before = snapshot(self.project)
                self.install()
                cache = self.project / "tools" / MODULE / "__pycache__"
                cache.mkdir()  # what Python leaves once the core ran the module
                (cache / "regulus.cpython-311.pyc").write_bytes(b"\0")
                self.remove()
                self.assertEqual(snapshot(self.project), before)

    def test_it_refuses_changed_or_unknown_files_and_changes_nothing(self):
        self.install()
        folder = self.project / "tools" / MODULE
        (folder / "run.py").write_text("# my edit\n")
        self.assert_refused(self.remove, "tools/text-extract/run.py changed since it was "
                                         "installed", "copy them out")
        shutil.copy(ROOT / "modules" / MODULE / "run.py", folder / "run.py")
        (folder / "notes.txt").write_text("mine\n")
        self.assert_refused(self.remove, "notes.txt was not installed by catalog.py")
        (folder / "notes.txt").unlink()
        (folder / "__pycache__").mkdir()
        (folder / "__pycache__" / "other.cpython-311.pyc").write_bytes(b"\0")
        self.assert_refused(self.remove, "__pycache__ was not installed")
        shutil.rmtree(folder / "__pycache__")
        self.remove()

    def test_it_refuses_an_entry_edited_by_hand(self):
        self.install()
        edited = self.toml().replace('version = "0.1.0"\n', 'version = "0.1.0"\nenv = { X = "1" }\n')
        self.write("regulus.toml", edited)
        self.assert_refused(self.remove, "entry `text-extract` in regulus.toml was changed")
        self.write("regulus.toml", edited.replace("# <<< regulus-modules: text-extract <<<\n", ""))
        self.assert_refused(self.remove, "was changed")

    def test_it_refuses_what_it_did_not_install(self):
        self.assert_refused(self.remove, "no folder tools/text-extract")
        self.write("regulus.toml", PROJECT + '\n[[modules.local]]\nid = "text-extract"\n'
                                             'command = ["python3", "tools/text-extract/run.py"]\n')
        self.write("tools/text-extract/run.py", "print('mine')\n")
        self.assert_refused(self.remove, "catalog.py did not install it")

    def test_an_entry_already_taken_out_by_hand_leaves_only_the_files_to_delete(self):
        self.install()
        self.write("regulus.toml", PROJECT)
        self.remove()
        self.assertFalse((self.project / "tools" / MODULE).exists())
        self.assertEqual(self.toml(), PROJECT)

    def test_entries_added_after_it_stay(self):
        self.install()
        later = '\n[[modules.local]]\nid = "later"\ncommand = ["python3", "later.py"]\n'
        self.write("regulus.toml", self.toml() + later)
        self.remove()
        self.assertEqual(self.toml(), PROJECT + later)


class Source(ProjectCase):
    """The catalogue side: only listed, unchanged, regular files inside the module."""

    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        shutil.copy(ROOT / "catalog.toml", self.clone / "catalog.toml")
        cache = shutil.ignore_patterns("__pycache__", "target")
        shutil.copytree(ROOT / "sdk", self.clone / "sdk", ignore=cache)
        shutil.copytree(ROOT / "modules", self.clone / "modules", ignore=cache)
        self.module = self.clone / "modules" / MODULE

    def install_from_clone(self):
        return self.install(self.clone)

    def test_a_changed_file_or_sdk_is_refused(self):
        (self.module / "run.py").write_text((self.module / "run.py").read_text() + "#\n")
        self.assert_refused(self.install_from_clone, "run.py: sha256 is")
        shutil.copy(ROOT / "modules" / MODULE / "run.py", self.module / "run.py")
        (self.clone / "sdk" / "regulus.py").write_text("evil = True\n")
        self.assert_refused(self.install_from_clone, "sdk/regulus.py: sha256 is")

    def test_paths_and_links_cannot_leave_the_module(self):
        manifest = self.module / "module.toml"
        good = manifest.read_text()
        manifest.write_text(good.replace('path = "run.py"', 'path = "../../catalog.toml"'))
        self.assert_refused(self.install_from_clone, "must be a file name in the module's folder")
        manifest.write_text(good.replace('path = "run.py"', 'path = "regulus.py"'))
        self.assert_refused(self.install_from_clone, "must be a file name")
        manifest.write_text(good)
        outside = self.tmp / "run.py"
        shutil.move(self.module / "run.py", outside)
        (self.module / "run.py").symlink_to(outside)
        self.assert_refused(self.install_from_clone, "must be a regular file")
        self.assert_refused(lambda: catalog.install("../modules", str(self.project), self.clone),
                            "is not a module id")
        self.assert_refused(lambda: catalog.install("absent", str(self.project), self.clone),
                            "no module `absent`")


class RustModule(ProjectCase):
    """A Rust module is a crate: files in folders, the helper as `src/regulus.rs`, a
    command that is the program `regulus node build` makes, and a `target/` that goes."""

    def install_rust(self, root=catalog.ROOT):
        return catalog.install(RUST_MODULE, str(self.project), root)

    def remove_rust(self):
        return catalog.remove(RUST_MODULE, str(self.project))

    def build(self):
        """What `regulus node build` leaves behind, as far as remove can tell."""
        self.write(f"tools/{RUST_MODULE}/target/release/node-{RUST_MODULE}", "program")
        self.write(f"tools/{RUST_MODULE}/target/release/deps/anything.d", "x")

    def test_it_installs_the_crate_with_the_helper_and_the_built_program_as_command(self):
        module, _, files = self.install_rust()
        self.assertEqual(module.lang, "rust")
        installed = self.project / "tools" / RUST_MODULE
        source = ROOT / "modules" / RUST_MODULE
        for name in module.files:
            self.assertEqual((installed / name).read_bytes(), (source / name).read_bytes(), name)
        for name in ("Cargo.toml", "Cargo.lock", "regulus-module.toml", ".gitignore",
                     "src/main.rs", "src/regulus.rs", ".regulus-modules.json"):
            self.assertIn(name, files)
        helper = installed / "src" / "regulus.rs"
        self.assertFalse(helper.is_symlink(), "the project gets the file, not this clone's link")
        self.assertEqual(helper.read_bytes(), (ROOT / "sdk" / "regulus.rs").read_bytes())
        self.assertFalse((installed / "target").exists(), "nothing is built or shipped built")
        self.assertFalse((installed / "regulus.py").exists())
        entry = tomllib.loads(self.toml())["modules"]["local"][-1]
        self.assertEqual(entry, {
            "id": RUST_MODULE, "version": module.version,
            "command": [f"tools/{RUST_MODULE}/target/release/node-{RUST_MODULE}"]})
        self.assert_refused(self.install_rust, "already has a [[modules.local]]")

    def test_install_build_remove_leaves_the_project_exactly_as_it_was(self):
        before = snapshot(self.project)
        self.install_rust()
        self.build()
        _, _, edited, kept = self.remove_rust()
        self.assertTrue(edited)
        self.assertFalse(kept)
        self.assertEqual(snapshot(self.project), before)
        self.assertEqual(self.toml(), PROJECT)

    def test_remove_refuses_changed_or_unknown_files_in_any_folder(self):
        self.install_rust()
        self.build()
        folder = self.project / "tools" / RUST_MODULE
        (folder / "src" / "main.rs").write_text("fn main() {}\n")
        self.assert_refused(self.remove_rust, f"tools/{RUST_MODULE}/src/main.rs changed since")
        shutil.copy(ROOT / "modules" / RUST_MODULE / "src" / "main.rs", folder / "src" / "main.rs")
        (folder / "src" / "mine.rs").write_text("// mine\n")
        self.assert_refused(self.remove_rust, f"tools/{RUST_MODULE}/src/mine.rs was not installed")
        (folder / "src" / "mine.rs").unlink()
        (folder / "notes").mkdir()
        self.assert_refused(self.remove_rust, f"tools/{RUST_MODULE}/notes was not installed")
        (folder / "notes").rmdir()
        shutil.rmtree(folder / "target")
        (folder / "target").symlink_to(self.tmp)
        self.assert_refused(self.remove_rust, f"tools/{RUST_MODULE}/target was not installed")
        (folder / "target").unlink()
        self.remove_rust()
        self.assertEqual(self.toml(), PROJECT)

    def test_a_python_module_may_not_keep_a_build_folder_nor_a_rust_one_bytecode(self):
        catalog.install(MODULE, str(self.project))
        self.write(f"tools/{MODULE}/target/x", "x")
        self.assert_refused(lambda: catalog.remove(MODULE, str(self.project)),
                            f"tools/{MODULE}/target was not installed")
        self.install_rust()
        self.write(f"tools/{RUST_MODULE}/__pycache__/run.cpython-311.pyc", "x")
        self.assert_refused(self.remove_rust, f"tools/{RUST_MODULE}/__pycache__ was not installed")


class RustSource(ProjectCase):
    """The catalogue side of a Rust module: its paths stay inside, and its crate is the one
    `regulus node build` can build."""

    def setUp(self):
        super().setUp()
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        shutil.copy(ROOT / "catalog.toml", self.clone / "catalog.toml")
        shutil.copy(ROOT / "README.md", self.clone / "README.md")
        ignore = shutil.ignore_patterns("__pycache__", "target")
        for folder in ("sdk", "modules", "template"):
            shutil.copytree(ROOT / folder, self.clone / folder, ignore=ignore, symlinks=True)
        self.module = self.clone / "modules" / RUST_MODULE
        self.manifest = self.module / "module.toml"
        self.good = self.manifest.read_text()

    def install_from_clone(self):
        return catalog.install(RUST_MODULE, str(self.project), self.clone)

    def check(self):
        with contextlib.redirect_stdout(io.StringIO()):
            catalog.command_check(None, self.clone)

    def test_the_copy_passes_and_installs(self):
        self.check()
        self.install_from_clone()

    def test_paths_cannot_leave_the_crate_or_name_what_install_and_build_write(self):
        for bad in ("../../catalog.toml", "src/../../x.rs", "/etc/passwd", "src/regulus.rs",
                    "target/release/node-scrapling", "src/.hidden", ".cargo/config.toml",
                    "a/b/c/d/e.rs", "src//main.rs", ""):
            self.manifest.write_text(self.good.replace('path = "src/tests.rs"',
                                                       f'path = "{bad}"'))
            self.assert_refused(self.install_from_clone, "[[file]] path")
        self.manifest.write_text(self.good.replace('lang = "rust"', 'lang = "go"'))
        self.assert_refused(self.install_from_clone, "`lang` 'go' must be one of python, rust")
        self.manifest.write_text(self.good.replace('lang = "rust"\n', ""))
        self.assert_refused(self.install_from_clone, "[[file]] path 'src/main.rs'")

    def test_a_changed_file_helper_or_link_is_refused(self):
        main = self.module / "src" / "main.rs"
        main.write_text(main.read_text() + "//\n")
        self.assert_refused(self.install_from_clone, "src/main.rs: sha256 is")
        shutil.copy(ROOT / "modules" / RUST_MODULE / "src" / "main.rs", main)
        helper = self.clone / "sdk" / "regulus.rs"
        original = helper.read_bytes()
        helper.write_text("// evil\n")
        self.assert_refused(self.install_from_clone, "sdk/regulus.rs: sha256 is")
        helper.write_bytes(original)
        link = self.module / "src" / "regulus.rs"
        link.unlink()
        link.write_text("// a copy of its own\n")
        with self.assertRaises(catalog.Refused) as caught:
            self.check()
        self.assertIn("src/regulus.rs must be a link to sdk/regulus.rs", str(caught.exception))

    def test_check_refuses_a_crate_node_build_would_not_accept(self):
        cargo = self.module / "Cargo.toml"
        text = cargo.read_text()
        for change, said in (
                (text.replace('name = "node-scrapling"', 'name = "scrapling"'),
                 "the package must be named `node-scrapling`"),
                (text.replace('version = "0.1.0"', 'version = "9.9.9"'),
                 "the package's version must be 0.1.0"),
                (text.replace("[workspace]", ""), "it needs an empty [workspace] table")):
            self.assertNotEqual(change, text)
            cargo.write_text(change)
            listed = self.good.replace(
                catalog.sha256((ROOT / "modules" / RUST_MODULE / "Cargo.toml").read_bytes()),
                catalog.sha256(change.encode()))
            self.manifest.write_text(listed)
            with self.assertRaises(catalog.Refused) as caught:
                self.check()
            self.assertIn(said, str(caught.exception))


class Check(unittest.TestCase):
    def test_this_repository_passes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            catalog.command_check(None)
        self.assertIn("ok:", out.getvalue())


class CommandLine(ProjectCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "catalog.py"), *args],
                              capture_output=True, text=True, timeout=60)

    def test_the_documented_commands(self):
        listed = self.run_cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("text-extract", listed.stdout)
        shown = self.run_cli("show", MODULE)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("extract_json", shown.stdout)
        self.assertIn("code_blocks", shown.stdout)
        installed = self.run_cli("install", MODULE, "--project", str(self.project))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn(f"regulus project trust {self.project}", installed.stdout)
        self.assertIn("regulus project reload demo --module text-extract", installed.stdout)
        again = self.run_cli("install", MODULE, "--project", str(self.project))
        self.assertEqual(again.returncode, 1)
        self.assertIn("refused:", again.stderr)
        removed = self.run_cli("remove", MODULE, "--project", str(self.project))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.toml(), PROJECT)

    def test_the_documented_commands_for_a_rust_module(self):
        listed = self.run_cli("list")
        self.assertIn(RUST_MODULE, listed.stdout)
        shown = self.run_cli("show", RUST_MODULE)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("Rust local module", shown.stdout)
        self.assertIn("fetch  (Scrapling fetch)", shown.stdout)
        self.assertIn("out:    out text@1, error text@1", shown.stdout)
        self.assertIn("src/regulus.rs", shown.stdout)
        installed = self.run_cli("install", RUST_MODULE, "--project", str(self.project))
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertIn(f"Read tools/{RUST_MODULE}/src/main.rs", installed.stdout)
        steps = [installed.stdout.index(step) for step in (
            f"regulus project trust {self.project}",
            f"regulus node build {self.project} --id {RUST_MODULE}",
            f"regulus project reload demo --module {RUST_MODULE}")]
        self.assertEqual(steps, sorted(steps), "trust, then build, then reload")
        removed = self.run_cli("remove", RUST_MODULE, "--project", str(self.project))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.toml(), PROJECT)


if __name__ == "__main__":
    unittest.main()
