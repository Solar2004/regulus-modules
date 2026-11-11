"""The catalogue against a real Regulus core, end to end. Skipped unless REGULUS_BIN names a
`regulus` binary (0.2.1 or newer):

    REGULUS_BIN=/path/to/regulus python3 -m unittest discover -s tests -p test_real_core.py -v

Everything happens in a temporary folder: its own `--home` (config, token, trust list)
and its own project, so the user's Regulus setup is never read or changed. The path:
install → open the project untrusted (the module is `blocked`, no process, a run is
refused) → trust it (`idle`) → reload it → the profile's ports match the module → a run
turns an agent-like reply into the JSON value and the code block → remove → reload (the
module is gone) → `regulus.toml` is byte for byte what it was.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGULUS = os.environ.get("REGULUS_BIN")
MODULE = "text-extract"

PROJECT = '# Demo project: this comment must survive.\n[project]\nname = "Demo"\n'

PROFILE = """\
id = "extract-pipe"
name = "extract-pipe"
main_workflow_id = "main"

[[workflows]]
id = "main"
name = "Extract"
kind = "main"
ports = [
  { id = "in", capability = "text@1", direction = "input", kind = "then" },
  { id = "out", capability = "json@1", direction = "output", kind = "then" },
  { id = "code", capability = "text@1", direction = "output", kind = "then" },
]

  [[workflows.nodes]]
  id = "json"
  name = "Extract JSON"
  type = "extract_json"
  module_id = "demo/text-extract"
  config = { path = "verdict" }
  ports = [
    { id = "in", capability = "text@1", direction = "input", kind = "then", cardinality = "many" },
    { id = "out", capability = "json@1", direction = "output", kind = "then" },
    { id = "missing", capability = "text@1", direction = "output", kind = "then" },
  ]

  [[workflows.nodes]]
  id = "code"
  name = "Code blocks"
  type = "code_blocks"
  module_id = "demo/text-extract"
  config = { language = "python", pick = "all" }
  ports = [
    { id = "in", capability = "text@1", direction = "input", kind = "then", cardinality = "many" },
    { id = "out", capability = "text@1", direction = "output", kind = "then" },
    { id = "count", capability = "json@1", direction = "output", kind = "then" },
  ]

  [[workflows.wires]]
  id = "w1"
  kind = "then"
  from = { node_id = "self", port_id = "in" }
  to = { node_id = "json", port_id = "in" }

  [[workflows.wires]]
  id = "w2"
  kind = "then"
  from = { node_id = "self", port_id = "in" }
  to = { node_id = "code", port_id = "in" }

  [[workflows.wires]]
  id = "w3"
  kind = "then"
  from = { node_id = "json", port_id = "out" }
  to = { node_id = "self", port_id = "out" }

  [[workflows.wires]]
  id = "w4"
  kind = "then"
  from = { node_id = "code", port_id = "out" }
  to = { node_id = "self", port_id = "code" }
"""

REPLY = 'The verdict:\n```json\n{"verdict": "pass", "score": 9}\n```\nFix:\n```python\nprint("ok")\n```'


def module_processes(project):
    """Command lines of running processes started from the project's copy of the module."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        if f"tools/{MODULE}/run.py" in cmdline and cwd == str(project):
            found.append(cmdline)
    return found


@unittest.skipUnless(REGULUS, "set REGULUS_BIN to a regulus binary to run against a real core")
class RealCore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="regulus-modules-core-"))
        self.home = self.tmp / "home"
        self.project = self.tmp / "demo"
        self.home.mkdir()
        (self.home / "config.toml").write_text("[server]\nport = 0\n")
        (self.project / ".regulus" / "profiles").mkdir(parents=True)
        (self.project / "regulus.toml").write_text(PROJECT)
        (self.project / ".regulus" / "profiles" / "extract-pipe.toml").write_text(PROFILE)
        self.log = open(self.tmp / "core.log", "wb")  # a file: a pipe nobody reads could fill
        self.core = subprocess.Popen([REGULUS, "--home", str(self.home), "serve"],
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        while self.regulus("module", "ls", check=False).returncode != 0:
            self.assertIsNone(self.core.poll(), "the core exited")
            self.assertLess(time.monotonic(), deadline, "the core did not start")
            time.sleep(0.2)

    def tearDown(self):
        self.core.terminate()
        try:
            self.core.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.core.kill()
        self.log.close()
        time.sleep(0.3)
        self.assertEqual(module_processes(self.project), [], "no module process left")
        shutil.rmtree(self.tmp)

    def regulus(self, *args, check=True):
        done = subprocess.run([REGULUS, "--home", str(self.home), *args],
                              capture_output=True, text=True, timeout=120)
        if check:
            self.assertEqual(done.returncode, 0, f"regulus {args}: {done.stdout}{done.stderr}")
        return done

    def catalog(self, *args):
        done = subprocess.run([sys.executable, str(ROOT / "catalog.py"), *args],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, f"catalog.py {args}: {done.stdout}{done.stderr}")
        return done.stdout

    def listed(self):
        lines = self.regulus("module", "ls").stdout.splitlines()
        return [line for line in lines if line.startswith(f"demo/{MODULE}")]

    def test_install_trust_run_remove(self):
        said = self.catalog("install", MODULE, "--project", str(self.project))
        self.assertIn(f"regulus project trust {self.project}", said)
        self.assertNotIn("trusted", (self.home / "config.toml").read_text())

        # Untrusted: listed as blocked, no process, the profile cannot run.
        self.regulus("project", "open", str(self.project))
        [line] = self.listed()
        self.assertIn("blocked", line)
        self.assertIn("untrusted project", line)
        self.assertEqual(module_processes(self.project), [])
        self.assertNotEqual(self.regulus("run", "extract-pipe", check=False).returncode, 0)

        # Trusted: it starts with the catalogue's version; a reload replaces the process.
        self.regulus("project", "trust", str(self.project))
        [line] = self.listed()
        self.assertTrue(line.startswith(f"demo/{MODULE}@0.1.0"), line)
        self.assertIn("idle", line)
        self.assertEqual(len(module_processes(self.project)), 1)
        reload = self.regulus("project", "reload", "demo", "--module", MODULE).stdout
        self.assertIn(f"replaced  demo/{MODULE}@0.1.0", reload)
        shown = self.regulus("module", "show", f"demo/{MODULE}").stdout
        self.assertIn('type = "extract_json"', shown)
        self.assertIn('type = "code_blocks"', shown)

        # The profile's ports are the module's, and a reply goes through both nodes.
        self.assertIn("no findings", self.regulus("profile", "check", "extract-pipe").stdout)
        self.assertIn("in sync", self.regulus("profile", "sync", "extract-pipe").stdout)
        execution = self.regulus("run", "extract-pipe", "--detach").stdout.split()[0]
        events = self.regulus("send", execution, "in", REPLY).stdout
        self.assertIn('self.out emitted json@1 "pass"', events)
        self.assertIn('self.code emitted text@1 "print(\\"ok\\")\\n"', events)
        self.assertIn("code.count emitted json@1 1", events)
        self.regulus("stop", execution)

        # Remove (with the core still running it), reload: gone, the file as it was.
        self.assertTrue((self.project / "tools" / MODULE / "__pycache__").is_dir(),
                        "the real run left Python's bytecode cache, which remove accepts")
        self.catalog("remove", MODULE, "--project", str(self.project))
        self.regulus("project", "reload", "demo")
        self.assertEqual(self.listed(), [])
        time.sleep(0.3)
        self.assertEqual(module_processes(self.project), [])
        self.assertEqual((self.project / "regulus.toml").read_text(), PROJECT)
        self.assertFalse((self.project / "tools").exists())


if __name__ == "__main__":
    unittest.main()
