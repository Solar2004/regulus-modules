//! scrapling: fetch a web page with the Scrapling CLI and send its content on.
//!
//! A Regulus local module in Rust: the core runs this program as a module of the project
//! (`[[modules.local]]` in `regulus.toml`) once the project is trusted, and talks to it
//! over stdin and stdout through `regulus.rs`, the helper beside this file. It is a
//! separate process, never linked into Regulus.
//!
//! Scrapling itself is not part of it: the module runs the `scrapling` command the user
//! installed (<https://github.com/D4Vinci/Scrapling>), once per page, as
//! `scrapling extract get|fetch <options> <url> <file>`, and reads the file back.
//!
//! One node type, `fetch`: a URL arrives on `in` and the page's content goes to `out` as
//! Markdown, HTML or plain text, the whole page or only what `selector` matches. `mode`
//! picks the fetcher: `http` is a plain request that looks like a browser's; `browser`
//! drives Scrapling's stealth browser. A page that could not be fetched, answered with an
//! HTTP error or gave no content goes to `error`, so a workflow can route it. A value that
//! is not an http(s) URL, a setting that makes no sense, or a `command` that cannot be
//! started is an error of the call instead.
//!
//! It opens the network (the pages asked for, through `proxy` if set), runs the
//! `scrapling` command, and writes the fetched page to a temporary folder it deletes. It
//! is not a sandbox: it runs with your permissions.
//!
//! Stdout belongs to the protocol: log with `eprintln!`, and every child process gets its
//! own stdin and stdout.

mod regulus;

use std::ffi::OsString;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use regulus::{Context, Error, Module};
use serde_json::Value;

const DEFAULT_COMMAND: &str = "scrapling";
const DEFAULT_TIMEOUT_MS: u64 = 30_000;
const MIN_TIMEOUT_MS: u64 = 1_000;
const MAX_TIMEOUT_MS: u64 = 600_000;
/// The CLI tries a plain request this many times before it gives up.
const ATTEMPTS: u32 = 3;
/// What the process gets on top of its attempts before it is killed: a browser takes a
/// while to start.
const MARGIN: Duration = Duration::from_secs(30);
/// How often a running CLI is looked at.
const POLL: Duration = Duration::from_millis(25);
/// How long the CLI's output is waited for once the process is gone.
const DRAIN: Duration = Duration::from_secs(5);
/// How much of the CLI's output an error quotes, in characters.
const MAX_ERROR_CHARS: usize = 500;
/// What the CLI logs once the page answered: `Fetched (200) <GET https://…>`.
const FETCHED: &str = "Fetched (";
const STATUS_DIGITS: usize = 3;
const FIRST_ERROR_STATUS: u16 = 400;
/// What the CLI's logger writes before the reason of a failure.
const LOGGED_ERROR: &str = "ERROR: ";
const TEMP_PREFIX: &str = "regulus-scrapling";

const MODES: [&str; 2] = ["http", "browser"];
const FORMATS: [&str; 3] = ["markdown", "html", "text"];
const YES_NO: [&str; 2] = ["no", "yes"];

/// The node types this program serves; `regulus-module.toml` declares their ports and
/// fields.
fn module() -> Module {
    Module::new(include_str!("../regulus-module.toml")).node("fetch", fetch)
}

fn main() {
    module().run();
}

/// A URL arrived on `in`: put the page's content on `out`, or why there is none on
/// `error`.
fn fetch(ctx: &Context, _port: &str, value: Value) -> Result<(), Error> {
    let url = page_url(&value)?;
    let settings = Settings::read(&ctx.node["config"])?;
    let failure = match fetch_page(&url, &settings)? {
        // A page too long for one message is said on `error` too.
        Ok(content) => match ctx.emit("out", content) {
            Ok(()) => return Ok(()),
            Err(error) => error.message().to_owned(),
        },
        Err(why) => why,
    };
    ctx.emit("error", format!("{}: {failure}", without_query(&url)))
}

/// The URL a value names: text that is one http(s) address, the spaces around it dropped.
fn page_url(value: &Value) -> Result<String, Error> {
    let url = value
        .as_str()
        .ok_or_else(|| Error::invalid("the URL must be text (text@1)"))?
        .trim();
    let host = url
        .strip_prefix("http://")
        .or_else(|| url.strip_prefix("https://"));
    let named = host.is_some_and(|rest| !rest.is_empty() && !rest.starts_with(['/', '?', '#']));
    if !named || url.chars().any(|c| c.is_whitespace() || c.is_control()) {
        let shown: String = url.chars().take(200).collect();
        return Err(Error::invalid(format!("not an http(s) URL: {shown:?}")));
    }
    Ok(url.to_owned())
}

/// `url` as errors say it: without its query and fragment, which may carry a key.
fn without_query(url: &str) -> &str {
    url.split(['?', '#']).next().unwrap_or(url)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Mode {
    Http,
    Browser,
}

/// The fields of a `fetch` node, read with their defaults (the core does not apply them).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Settings {
    mode: Mode,
    /// The extension of the file the CLI writes: it picks the output format from it.
    extension: &'static str,
    selector: String,
    main_content: bool,
    timeout_ms: u64,
    proxy: String,
    wait_selector: String,
    network_idle: bool,
    solve_cloudflare: bool,
    real_chrome: bool,
    /// The words that start the CLI: the program, then its first arguments.
    command: Vec<String>,
}

impl Settings {
    fn read(config: &Value) -> Result<Self, Error> {
        let mode = match choice(config, "mode", &MODES)? {
            "browser" => Mode::Browser,
            _ => Mode::Http,
        };
        let extension = match choice(config, "format", &FORMATS)? {
            "html" => ".html",
            "text" => ".txt",
            _ => ".md",
        };
        let mut command = words(&text(config, "command"))
            .map_err(|why| Error::invalid(format!("command cannot be read: {why}")))?;
        if command.is_empty() {
            command.push(DEFAULT_COMMAND.to_owned());
        }
        Ok(Self {
            mode,
            extension,
            selector: text(config, "selector"),
            main_content: yes(config, "main_content")?,
            timeout_ms: timeout_ms(&config["timeout_ms"])?,
            proxy: text(config, "proxy"),
            wait_selector: text(config, "wait_selector"),
            network_idle: yes(config, "network_idle")?,
            solve_cloudflare: yes(config, "solve_cloudflare")?,
            real_chrome: yes(config, "real_chrome")?,
            command,
        })
    }

    /// The CLI's arguments after the command: `extract get|fetch <options> <url> <file>`.
    /// Every option that takes a value is written `--name=value`, so no value is read as
    /// another option.
    fn cli_args(&self, url: &str, output: &Path) -> Vec<OsString> {
        let mut args: Vec<String> = vec!["extract".into()];
        match self.mode {
            // The plain request counts its timeout in seconds, the browser in milliseconds.
            Mode::Http => {
                args.push("get".into());
                args.push(format!("--timeout={}", self.timeout_ms.div_ceil(1000)));
            }
            Mode::Browser => {
                args.push("fetch".into());
                args.push(format!("--timeout={}", self.timeout_ms));
            }
        }
        let mut valued = vec![("--css-selector", &self.selector), ("--proxy", &self.proxy)];
        let mut flags = vec![("--ai-targeted", self.main_content)];
        if self.mode == Mode::Browser {
            valued.push(("--wait-selector", &self.wait_selector));
            flags.push(("--network-idle", self.network_idle));
            flags.push(("--solve-cloudflare", self.solve_cloudflare));
            flags.push(("--real-chrome", self.real_chrome));
        }
        args.extend(
            valued
                .into_iter()
                .filter(|(_, value)| !value.is_empty())
                .map(|(option, value)| format!("{option}={value}")),
        );
        args.extend(
            flags
                .into_iter()
                .filter(|(_, on)| *on)
                .map(|(flag, _)| flag.to_owned()),
        );
        args.push(url.to_owned());
        let mut args: Vec<OsString> = args.into_iter().map(OsString::from).collect();
        args.push(output.as_os_str().to_owned());
        args
    }

    /// How long the CLI may run before it is killed.
    fn deadline(&self) -> Duration {
        Duration::from_millis(self.timeout_ms) * ATTEMPTS + MARGIN
    }
}

/// The field `id` as text, trimmed: "" when it is not set.
fn text(config: &Value, id: &str) -> String {
    match &config[id] {
        Value::Null => String::new(),
        Value::String(text) => text.trim().to_owned(),
        other => other.to_string(),
    }
}

/// The field `id`, which must be one of `options`; the first one when it is not set.
fn choice<'a>(config: &Value, id: &str, options: &[&'a str]) -> Result<&'a str, Error> {
    let value = text(config, id);
    if value.is_empty() {
        return Ok(options[0]);
    }
    options
        .iter()
        .copied()
        .find(|option| *option == value)
        .ok_or_else(|| {
            Error::invalid(format!(
                "{id} must be one of {}, not {value:?}",
                options.join(", ")
            ))
        })
}

fn yes(config: &Value, id: &str) -> Result<bool, Error> {
    Ok(choice(config, id, &YES_NO)? == "yes")
}

/// The `timeout_ms` field: a number (or a number as text) within the limits.
fn timeout_ms(value: &Value) -> Result<u64, Error> {
    let number = match value {
        Value::Null => return Ok(DEFAULT_TIMEOUT_MS),
        Value::String(text) if text.trim().is_empty() => return Ok(DEFAULT_TIMEOUT_MS),
        Value::String(text) => text.trim().parse::<f64>().ok(),
        other => other.as_f64(),
    };
    match number {
        Some(ms) if ms >= MIN_TIMEOUT_MS as f64 && ms <= MAX_TIMEOUT_MS as f64 => Ok(ms as u64),
        _ => Err(Error::invalid(format!(
            "timeout_ms must be a number from {MIN_TIMEOUT_MS} to {MAX_TIMEOUT_MS}, not {value}"
        ))),
    }
}

/// The words of a command line: split at spaces, with `'…'` and `"…"` keeping theirs. No
/// escapes and no expansion: it is not a shell.
fn words(line: &str) -> Result<Vec<String>, String> {
    let mut found = Vec::new();
    let mut word = String::new();
    let mut started = false;
    let mut quote = None;
    for c in line.chars() {
        match quote {
            Some(open) if c == open => quote = None,
            Some(_) => word.push(c),
            None if c == '\'' || c == '"' => {
                quote = Some(c);
                started = true;
            }
            None if c.is_whitespace() => {
                if started {
                    found.push(std::mem::take(&mut word));
                    started = false;
                }
            }
            None => {
                word.push(c);
                started = true;
            }
        }
    }
    if let Some(open) = quote {
        return Err(format!("a {open} is never closed"));
    }
    if started {
        found.push(word);
    }
    Ok(found)
}

/// `program` with a leading `~/` replaced by the home folder.
fn expand_home(program: &str) -> PathBuf {
    match (program.strip_prefix("~/"), std::env::var_os("HOME")) {
        (Some(rest), Some(home)) => Path::new(&home).join(rest),
        _ => PathBuf::from(program),
    }
}

/// A folder of this process alone, deleted with everything in it when dropped.
struct TempDir(PathBuf);

impl TempDir {
    fn new() -> io::Result<Self> {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |since| since.subsec_nanos());
        let name = format!(
            "{TEMP_PREFIX}-{}-{nanos}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        );
        let path = std::env::temp_dir().join(name);
        let mut builder = std::fs::DirBuilder::new();
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            builder.mode(0o700);
        }
        // Not recursive: a folder that already exists (someone else's) is an error.
        builder.create(&path)?;
        Ok(Self(path))
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Kills every process of the group the CLI leads: a browser fetch is several processes.
/// The standard library cannot signal a group, so the system's `kill` does it.
#[cfg(unix)]
fn kill_group(leader: u32) {
    let _ = Command::new("kill")
        .args(["-KILL", "--", &format!("-{leader}")])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

#[cfg(not(unix))]
fn kill_group(_leader: u32) {}

/// Runs the CLI: its exit status (`None` if it ran past `deadline` and was killed) and
/// what it wrote, stdout then stderr.
fn run_cli(
    command: &[String],
    args: &[OsString],
    deadline: Duration,
) -> io::Result<(Option<ExitStatus>, String)> {
    let mut cli = Command::new(expand_home(&command[0]));
    cli.args(&command[1..])
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cli.process_group(0);
    }
    let mut child = cli.spawn()?;
    let (sender, written) = mpsc::channel();
    let streams: [Option<Box<dyn Read + Send>>; 2] = [
        child.stdout.take().map(|s| Box::new(s) as _),
        child.stderr.take().map(|s| Box::new(s) as _),
    ];
    for (index, stream) in streams.into_iter().enumerate() {
        let Some(mut stream) = stream else { continue };
        let sender = sender.clone();
        thread::spawn(move || {
            let mut bytes = Vec::new();
            let _ = stream.read_to_end(&mut bytes);
            let _ = sender.send((index, bytes));
        });
    }
    drop(sender);

    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break Some(status);
        }
        if started.elapsed() >= deadline {
            kill_group(child.id());
            let _ = child.kill();
            let _ = child.wait();
            break None;
        }
        thread::sleep(POLL);
    };
    // A process the CLI left behind may hold a pipe open: do not wait for it forever.
    let mut parts = [Vec::new(), Vec::new()];
    let drained = Instant::now() + DRAIN;
    while let Ok((index, bytes)) =
        written.recv_timeout(drained.saturating_duration_since(Instant::now()))
    {
        parts[index] = bytes;
    }
    let log = parts
        .iter()
        .map(|bytes| String::from_utf8_lossy(bytes))
        .collect::<String>();
    Ok((status, log))
}

/// The HTTP status the CLI logged for the page (the last one), if it logged any.
fn status_of(log: &str) -> Option<u16> {
    let after = &log[log.rfind(FETCHED)? + FETCHED.len()..];
    let digits = after.get(..STATUS_DIGITS)?;
    if !after[STATUS_DIGITS..].starts_with(')') {
        return None;
    }
    digits.parse().ok()
}

/// Whether `line` ends a Python traceback: `package.SomeError: why`.
fn names_exception(line: &str) -> bool {
    line.split_once(": ").is_some_and(|(name, _)| {
        (name.ends_with("Error") || name.ends_with("Exception"))
            && name
                .chars()
                .all(|c| c.is_alphanumeric() || c == '.' || c == '_')
    })
}

/// The line of the CLI's output that says why it failed, cut to a readable length: the
/// exception that ended it, else the last error it logged, else its last line.
fn error_of(log: &str) -> String {
    let lines: Vec<&str> = log
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    let exception = lines.iter().rev().find(|line| names_exception(line));
    let logged = lines
        .iter()
        .rev()
        .find_map(|line| line.split_once(LOGGED_ERROR).map(|(_, why)| why));
    let line = exception
        .copied()
        .or(logged)
        .or(lines.last().copied())
        .unwrap_or("the scrapling command wrote nothing");
    line.chars().take(MAX_ERROR_CHARS).collect()
}

/// Fetches `url` as `settings` say: the content, or why there is none (for `error`). An
/// `Err` outside is the call's own: the CLI could not be started at all.
fn fetch_page(url: &str, settings: &Settings) -> Result<Result<String, String>, Error> {
    let folder = TempDir::new()
        .map_err(|error| Error::failed(format!("cannot make a temporary folder: {error}")))?;
    let output = folder.0.join(format!("page{}", settings.extension));
    let ran = run_cli(
        &settings.command,
        &settings.cli_args(url, &output),
        settings.deadline(),
    );
    let (status, log) = ran.map_err(|error| {
        let program = &settings.command[0];
        if error.kind() == io::ErrorKind::NotFound {
            Error::failed(format!(
                "the Scrapling CLI `{program}` was not found: install Scrapling \
                 (https://github.com/D4Vinci/Scrapling) and set this node's `command` to \
                 the full path of its `scrapling` program"
            ))
        } else {
            Error::failed(format!("cannot run `{program}`: {error}"))
        }
    })?;
    let Some(status) = status else {
        return Ok(Err("the fetch took too long and was stopped".into()));
    };
    if !status.success() {
        return Ok(Err(error_of(&log)));
    }
    if let Some(code) = status_of(&log).filter(|code| *code >= FIRST_ERROR_STATUS) {
        return Ok(Err(format!("the page answered HTTP {code}")));
    }
    let Ok(bytes) = std::fs::read(&output) else {
        return Ok(Err("the scrapling command saved no page".into()));
    };
    let content = String::from_utf8_lossy(&bytes).into_owned();
    if content.trim().is_empty() {
        return Ok(Err(if settings.selector.is_empty() {
            "the page is empty".into()
        } else {
            "nothing matched the selector".into()
        }));
    }
    Ok(Ok(content))
}

#[cfg(test)]
mod tests;
