"""Tests of my-module. Run: python3 -m unittest discover -s modules/my-module/tests"""
import importlib.util
import sys
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1]
ROOT = next(p for p in MODULE.parents if (p / "catalog.py").exists())
sys.path.insert(0, str(ROOT / "sdk"))

_spec = importlib.util.spec_from_file_location("my_module_run", MODULE / "run.py")
run = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run)


class FakeContext:
    """What a handler uses of the SDK's context: its config and emit."""

    def __init__(self, **config):
        self.config = config
        self.sent = []

    def emit(self, port, value):
        self.sent.append((port, value))


class MyNode(unittest.TestCase):
    def test_it_sends_the_text_after_the_prefix(self):
        ctx = FakeContext(prefix="> ")
        run.my_node(ctx, "in", "hello")
        self.assertEqual(ctx.sent, [("out", "> hello")])

    def test_it_refuses_what_is_not_text(self):
        with self.assertRaises(run.rg.InvalidInput):
            run.my_node(FakeContext(prefix=""), "in", 42)


if __name__ == "__main__":
    unittest.main()
