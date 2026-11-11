"""Tests of text-extract: the parsing on its own, the node handlers, and the module as the
core runs it (a process speaking the protocol over stdin/stdout, with the SDK beside it).

Run: python3 -m unittest discover -s modules/text-extract/tests
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
ROOT = next(p for p in MODULE.parents if (p / "catalog.py").exists())
SDK = ROOT / "sdk" / "regulus.py"
sys.path.insert(0, str(SDK.parent))

_spec = importlib.util.spec_from_file_location("text_extract_run", MODULE / "run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)

REPLY = """Sure. Here is the verdict:

```json
{"verdict": "pass", "scores": [7, 9], "notes": {"a/b": "slash", "t~x": "tilde"}}
```

And a script:

~~~python
print("hello")
~~~

```sh
echo done
```
"""


class FakeContext:
    """What a handler uses of the SDK's context: its config and emit."""

    def __init__(self, **config):
        self.config = config
        self.sent = []

    def emit(self, port, value):
        self.sent.append((port, value))


class FindJson(unittest.TestCase):
    def test_whole_text_fenced_block_or_prose(self):
        self.assertEqual(run.find_json(' {"a": 1} '), {"a": 1})
        self.assertEqual(run.find_json(REPLY)["verdict"], "pass")
        self.assertEqual(run.find_json("```\n[1, 2]\n```"), [1, 2])
        self.assertEqual(run.find_json('As said in [1], the answer is {"ok": true}.'),
                         {"ok": True}, "an object wins over an earlier array")
        self.assertEqual(run.find_json("items: [\"a\", \"b\"] done"), ["a", "b"])

    def test_json_null_is_a_value_and_no_json_is_missing(self):
        self.assertIsNone(run.find_json("null"))
        self.assertIs(run.find_json("no json {here"), run.MISSING)
        self.assertIs(run.find_json(""), run.MISSING)
        self.assertIs(run.find_json("```python\nx = [\n```"), run.MISSING)

    def test_a_block_in_another_language_is_not_read_as_json(self):
        text = "```python\n[3]\n```\n```json\n{\"b\": 2}\n```"
        self.assertEqual(run.find_json(text), {"b": 2})

    def test_pathological_text_stays_bounded(self):
        self.assertIs(run.find_json("[" * 100000), run.MISSING)
        self.assertIs(run.find_json('{"a' * 20000), run.MISSING)


class AtPath(unittest.TestCase):
    value = {"a": {"b": [10, {"c": "deep"}]}, "x/y": 1, "t~": 2, "": 3}

    def test_dotted_paths_and_pointers(self):
        self.assertEqual(run.at_path(self.value, "a.b.1.c"), "deep")
        self.assertEqual(run.at_path(self.value, "/a/b/0"), 10)
        self.assertEqual(run.at_path(self.value, "/x~1y"), 1)
        self.assertEqual(run.at_path(self.value, "/t~0"), 2)
        self.assertEqual(run.at_path(self.value, "/"), 3)
        self.assertIs(run.at_path(self.value, ""), self.value)
        self.assertIs(run.at_path(self.value, " . "), self.value)

    def test_what_is_not_there_is_missing(self):
        for path in ("a.z", "/a/b/2", "a.b.01", "a.b.-1", "a.b.c", "a.b.1.c.d"):
            self.assertIs(run.at_path(self.value, path), run.MISSING, path)


class FencedBlocks(unittest.TestCase):
    def test_languages_fences_and_indent(self):
        blocks = run.fenced_blocks(REPLY)
        self.assertEqual([lang for lang, _ in blocks], ["json", "python", "sh"])
        self.assertEqual(blocks[1][1], 'print("hello")\n')
        indented = run.fenced_blocks("  ````Python title=x\n  a\n    b\n  ```\n  ````\n")
        self.assertEqual(indented, [("python", "a\n  b\n```\n")])

    def test_unclosed_block_runs_to_the_end_and_inline_ticks_are_not_fences(self):
        self.assertEqual(run.fenced_blocks("```sh\nls\n"), [("sh", "ls\n")])
        self.assertEqual(run.fenced_blocks("``` a`b\nx\n"), [])

    def test_pick_by_language_and_position(self):
        blocks = run.fenced_blocks(REPLY)
        self.assertEqual(run.pick_blocks(blocks, "sh, Python", "all"),
                         ['print("hello")\n', "echo done\n"])
        self.assertEqual(run.pick_blocks(blocks, "", "last"), ["echo done\n"])
        self.assertEqual(run.pick_blocks(blocks, "rust", "first"), [])
        with self.assertRaises(run.rg.InvalidInput):
            run.pick_blocks(blocks, "", "middle")


class Handlers(unittest.TestCase):
    def test_extract_json_sends_the_value_or_the_text_on_missing(self):
        ctx = FakeContext(path="scores.1")
        run.extract_json(ctx, "in", REPLY)
        self.assertEqual(ctx.sent, [("out", 9)])
        ctx = FakeContext(path="nope")
        run.extract_json(ctx, "in", REPLY)
        self.assertEqual(ctx.sent, [("missing", REPLY)])
        ctx = FakeContext(path="")
        run.extract_json(ctx, "in", {"already": "json"})
        self.assertEqual(ctx.sent, [("out", {"already": "json"})])

    def test_code_blocks_sends_each_block_then_the_count(self):
        ctx = FakeContext(language="", pick="all")
        run.code_blocks(ctx, "in", REPLY)
        self.assertEqual([port for port, _ in ctx.sent], ["out", "out", "out", "count"])
        self.assertEqual(ctx.sent[-1], ("count", 3))
        with self.assertRaises(run.rg.InvalidInput):
            run.code_blocks(FakeContext(language="", pick="all"), "in", 42)


class Protocol(unittest.TestCase):
    """The module as installed (`run.py` with `regulus.py` beside it), driven like the core."""

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="text-extract-"))
        shutil.copy(MODULE / "run.py", self.folder / "run.py")
        shutil.copy(SDK, self.folder / "regulus.py")

    def tearDown(self):
        shutil.rmtree(self.folder)

    def converse(self, requests):
        lines = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests)
        done = subprocess.run([sys.executable, str(self.folder / "run.py")],
                              input=lines.encode(), capture_output=True, timeout=30,
                              cwd=self.folder)
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return [json.loads(line) for line in done.stdout.decode().splitlines() if line]

    @staticmethod
    def event(request_id, node_type, config, value):
        node = {"id": "n", "type": node_type, "config": config}
        return {"id": request_id, "method": "call", "params": {
            "method": "node/event",
            "params": {"execution_id": "exec-1", "node": node,
                       "args": {"port": "in", "capability": "text@1", "value": value}}}}

    def test_manifest_events_and_errors_over_stdio(self):
        replies = self.converse([
            {"id": 1, "method": "manifest", "params": {"protocol": "1.17"}},
            {"id": 2, "method": "start", "params": {"project_id": "demo", "root": "/tmp"}},
            self.event(3, "extract_json", {"path": "/verdict"}, REPLY),
            self.event(4, "code_blocks", {"language": "python"}, REPLY),
            self.event(5, "code_blocks", {}, ["not", "text"]),
            {"id": 6, "method": "stop", "params": {}},
        ])
        by_id = {r["id"]: r for r in replies if "id" in r}
        emits = [(r["params"]["port"], r["params"]["value"])
                 for r in replies if r.get("method") == "emit"]
        manifest = by_id[1]["result"]
        self.assertEqual(manifest["module"]["name"], "Text extract")
        self.assertEqual([n["type"] for n in manifest["node"]], ["extract_json", "code_blocks"])
        self.assertIn(("out", "pass"), emits)
        self.assertIn(("out", 'print("hello")\n'), emits)
        self.assertIn(("count", 1), emits)
        self.assertEqual(by_id[5]["error"]["code"], -32602)
        self.assertIsNone(by_id[6]["result"])


if __name__ == "__main__":
    unittest.main()
