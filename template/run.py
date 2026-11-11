#!/usr/bin/env python3
"""my-module: one line on what it does.

A Regulus local module written with the Python SDK (`regulus.py`, installed beside this
file; standard library only). Say what each node type does with what arrives, what it
sends, and anything it touches outside the process (files, commands, network).

Regulus runs it only in a project the user trusts, with the user's permissions: it is
not a sandbox.
"""
import regulus as rg


@rg.node(
    "my_node",
    name="My node",
    inputs=[rg.then_in("in", "text@1")],
    outputs=[rg.then_out("out", "text@1")],
    fields=[rg.field("prefix", "text", default="", label="Prefix",
                     description="Text put before each value.")],
)
def my_node(ctx, port, value):
    """A text arrived on `in`: send it on `out` after the prefix."""
    if not isinstance(value, str):
        raise rg.InvalidInput("text@1 must be a string")
    ctx.emit("out", ctx.config["prefix"] + value)


if __name__ == "__main__":
    rg.run(name="My module")
