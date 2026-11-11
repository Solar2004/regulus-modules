# scrapling

A workflow often needs a page a plain request cannot read: it is drawn by JavaScript, or
the site blocks anything that does not look like a browser. `scrapling` hands the URL to
[Scrapling](https://github.com/D4Vinci/Scrapling), which fetches it as a browser would (or
with a real stealth browser), and sends the page on as Markdown, HTML or text, the whole
page or only what a CSS selector matches. Wire a URL into it and its `out` into an agent's
prompt, a `text-extract` node or a file.

It complements the built-in `web.fetch`, and has the same ports (`in`, `out`, `error`), so
one replaces the other: `web.fetch` is inside Regulus, needs nothing installed and guards
against private addresses; `scrapling` gets through where that one is refused.

It is a **Rust** module: a small program Regulus runs beside the core. It does not contain
Scrapling: it runs the `scrapling` command you installed, once per page.

| Node type | Input | Outputs | Fields |
|---|---|---|---|
| `fetch` | `in` text@1 (a URL) | `out` text@1 · `error` text@1 | see below |

## Fields

| Field | Default | What it does |
|---|---|---|
| `mode` | `http` | `http`: a plain request that looks like a browser's (fast, no JavaScript). `browser`: Scrapling's stealth browser. |
| `format` | `markdown` | What goes to `out`: `markdown`, `html` or `text`. |
| `selector` | `""` | Only what this CSS selector matches (every match). Empty: the whole page. |
| `main_content` | `no` | `yes`: drop navigation, hidden elements and other noise, for a reader that is a model. |
| `timeout_ms` | `30000` | Milliseconds one attempt may take, 1000 to 600000. |
| `proxy` | `""` | `http://user:password@host:port`, or `secret:<name>`. |
| `wait_selector` | `""` | `browser` only: wait until this CSS selector is on the page. |
| `network_idle` | `no` | `browser` only: wait until the page stops loading things. |
| `solve_cloudflare` | `no` | `browser` only: pass Cloudflare's challenge page. |
| `real_chrome` | `no` | `browser` only: drive the Chrome installed on this computer instead of the browser `scrapling install` downloads. |
| `command` | `scrapling` | The Scrapling CLI: see below. |

## Example

```
in: https://example.com/pricing
mode = "http"  format = "markdown"  selector = "main"
  →  out:   "Pricing\n=======\n\n…"

in: https://example.com/gone?key=abc
  →  error: "https://example.com/gone: the page answered HTTP 404"

in: not a url
  →  the call fails (invalid params); nothing is sent
```

A page goes to `error`, as `<the URL without its query>: <why>`, when it could not be
fetched, answered with an HTTP status of 400 or more, gave no content (or nothing matched
`selector`), took too long, or is too long for one message. A value that is not one
http(s) URL, a field with a value it does not take, or a `command` that cannot be started
fails the call instead: that is the workflow's mistake, not the page's.

## What it needs

- **Scrapling's command line**, installed by you: `pip install "scrapling[shell]"` in an
  environment of yours ([Scrapling's instructions](https://github.com/D4Vinci/Scrapling#installation));
  `scrapling extract get --help` must work. `mode = browser` also needs the browser that
  `scrapling install` downloads, or `real_chrome = yes` and Chrome. Tested with
  Scrapling 0.5.
- **The `command` field pointing at it.** Regulus starts modules with its own `PATH`, not
  your shell's: if `scrapling` lives in a virtual environment, write its full path, such
  as `~/.venvs/scrapling/bin/scrapling` (`which scrapling` in your shell prints it).
  Several words work too, such as `uvx scrapling`; quote a word that has spaces.
- **`cargo`** (a recent stable Rust; built here with 1.95) to build it, and the network
  the first time, for its one dependency, `serde_json`.

## In a project

Into an existing Regulus project (a folder with a `regulus.toml`; the
[top-level README](../../README.md#use-a-module) has the full setup, including the core
that must be running for the `regulus` commands):

```sh
python3 catalog.py install scrapling --project <project-folder>
regulus project trust <project-folder>
regulus node build <project-folder> --id scrapling
regulus project reload <project-id> --module scrapling
```

Its node then shows up in the app's palette. In a profile file it is
`module_id = "<project-id>/scrapling"` with `type = "fetch"`.

## Limits and what it touches

- **The network**: it fetches the URLs it is given, through `proxy` if set. Unlike
  `web.fetch` it does **not** refuse private or local addresses: a workflow that feeds it
  URLs from a stranger can reach your own network.
- **A command**: it runs `command` with the URL and the fields as arguments. `proxy` is
  one of those arguments, so its password shows in the process list while a fetch runs.
- **Files**: Scrapling saves the page to a temporary folder (`regulus-scrapling-…`, only
  yours to read), which the module reads and deletes.
- One `scrapling` process per page, started and ended each time: a browser fetch takes
  seconds, and nothing (cookies, a login) is kept between pages.
- The HTTP status is read from what the CLI logs (`Fetched (404)`); if a later Scrapling
  stops logging it, an error page would go to `out` like any other.
- A fetch that runs past three times `timeout_ms` plus 30 seconds is killed with every
  process it started. A fetch still running when the module is stopped or reloaded ends on
  its own, within that time.
- Linux and macOS. Tested with Regulus 0.2.1 and Scrapling 0.5, with a plain request and
  with `real_chrome`.
