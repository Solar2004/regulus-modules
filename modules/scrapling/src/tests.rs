//! Tests of the `scrapling` module: the settings and the CLI's arguments, what is read
//! from the CLI's output, and the node driven over the protocol against a stand-in for
//! the `scrapling` command (a shell script, so no network and no Scrapling are needed).

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use super::*;

/// What stands in for the Scrapling CLI: it writes its arguments beside itself, then acts
/// on the last segment of the URL.
const FAKE_CLI: &str = r#"
for arg; do url=$out; out=$arg; done
printf '%s\n' "$@" > "$0.args"
case "$url" in
  */ok*)      echo "[now] INFO: Fetched (200) <GET $url>" >&2; printf '# Hello\n' > "$out" ;;
  */missing*) echo "[now] INFO: Fetched (404) <GET $url>" >&2; printf 'Not found' > "$out" ;;
  */empty*)   echo "[now] INFO: Fetched (200) <GET $url>" >&2; : > "$out" ;;
  */nofile*)  echo "[now] INFO: Fetched (200) <GET $url>" >&2 ;;
  */boom*)    echo "Traceback (most recent call last):" >&2
             echo "curl_cffi.curl.CurlError: Failed to connect" >&2
             echo "+-- a box the CLI draws last --+" >&2
             exit 1 ;;
  */slow*)    sleep 30 ;;
esac
"#;

/// A folder with the stand-in CLI in it. It is run as `sh <script>`: a script written and
/// then executed directly can fail with "text file busy" while other tests start processes.
struct Fake(TempDir);

impl Fake {
    fn new() -> Self {
        let folder = TempDir::new().unwrap();
        std::fs::write(folder.0.join("scrapling"), FAKE_CLI).unwrap();
        Self(folder)
    }

    fn script(&self) -> PathBuf {
        self.0 .0.join("scrapling")
    }

    /// The `command` field that runs it.
    fn command(&self) -> String {
        format!("sh '{}'", self.script().display())
    }

    /// The arguments of its last run.
    fn args(&self) -> Vec<String> {
        let recorded = self.script().with_extension("args");
        let text = std::fs::read_to_string(recorded).unwrap();
        text.lines().map(str::to_owned).collect()
    }
}

fn settings(config: Value) -> Settings {
    Settings::read(&config).unwrap()
}

fn args(config: Value) -> Vec<String> {
    settings(config)
        .cli_args("https://example.com/a?b=1", Path::new("/tmp/page.md"))
        .into_iter()
        .map(|arg| arg.into_string().unwrap())
        .collect()
}

/// What the module writes for `requests`, one event at a time.
fn exchange(requests: &[Value]) -> Vec<Value> {
    let input: String = requests.iter().map(|r| format!("{r}\n")).collect();
    let mut output = Vec::new();
    module().workers(1).serve(input.as_bytes(), &mut output);
    output
        .split(|b| *b == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice(line).unwrap())
        .collect()
}

fn event(id: u64, value: Value, config: Value) -> Value {
    json!({
        "jsonrpc": "2.0", "id": id, "method": "call",
        "params": { "method": "node/event", "params": {
            "execution_id": "exec-1",
            "node": { "id": "n1", "type": "fetch", "config": config },
            "args": { "port": "in", "capability": "text@1", "value": value },
        }},
    })
}

/// The one value a call emitted, as (port, value), and the call's answer.
fn emitted(out: &[Value]) -> (String, String) {
    assert_eq!(out.len(), 2, "{out:?}");
    assert_eq!(out[0]["method"], "emit", "{out:?}");
    assert_eq!(out[1]["result"], Value::Null, "{out:?}");
    let params = &out[0]["params"];
    (
        params["port"].as_str().unwrap().to_owned(),
        params["value"].as_str().unwrap().to_owned(),
    )
}

#[test]
fn the_manifest_declares_the_node_and_every_field_the_code_reads() {
    let out = exchange(&[json!({ "jsonrpc": "2.0", "id": 1, "method": "manifest" })]);
    let text = out[0]["result"].as_str().unwrap();
    assert!(text.contains("type = \"fetch\""), "{text}");
    for field in [
        "mode",
        "format",
        "selector",
        "main_content",
        "timeout_ms",
        "proxy",
        "wait_selector",
        "network_idle",
        "solve_cloudflare",
        "real_chrome",
        "command",
    ] {
        assert!(text.contains(&format!("id      = \"{field}\"")), "{field}");
    }
    for options in [&MODES[..], &FORMATS[..], &YES_NO[..]] {
        let listed = format!("options = {options:?}");
        assert!(text.contains(&listed), "{listed}");
    }
}

#[test]
fn unset_fields_take_the_defaults_of_the_manifest() {
    let read = settings(json!({}));
    assert_eq!(read.mode, Mode::Http);
    assert_eq!(read.extension, ".md");
    assert_eq!(read.timeout_ms, DEFAULT_TIMEOUT_MS);
    assert_eq!(read.command, ["scrapling"]);
    assert_eq!(read, settings(Value::Null), "a node with no config at all");
    assert_eq!(
        args(json!({})),
        [
            "extract",
            "get",
            "--timeout=30",
            "https://example.com/a?b=1",
            "/tmp/page.md"
        ]
    );
}

#[test]
fn http_arguments_carry_the_options_as_one_word_each() {
    let got = args(json!({
        "selector": "-p .a", "proxy": "http://u:p@h:1", "main_content": "yes",
        "timeout_ms": 1500,
        // Browser-only fields are not passed to a plain request.
        "wait_selector": "#x", "network_idle": "yes",
    }));
    assert_eq!(
        got,
        [
            "extract",
            "get",
            "--timeout=2",
            "--css-selector=-p .a",
            "--proxy=http://u:p@h:1",
            "--ai-targeted",
            "https://example.com/a?b=1",
            "/tmp/page.md"
        ]
    );
}

#[test]
fn browser_arguments_count_milliseconds_and_add_the_browser_options() {
    let got = args(json!({
        "mode": "browser", "format": "html", "timeout_ms": "45000",
        "wait_selector": "#app", "network_idle": "yes", "solve_cloudflare": "yes",
        "real_chrome": "yes",
    }));
    assert_eq!(
        got,
        [
            "extract",
            "fetch",
            "--timeout=45000",
            "--wait-selector=#app",
            "--network-idle",
            "--solve-cloudflare",
            "--real-chrome",
            "https://example.com/a?b=1",
            "/tmp/page.md"
        ]
    );
    assert_eq!(settings(json!({ "format": "html" })).extension, ".html");
    assert_eq!(settings(json!({ "format": "text" })).extension, ".txt");
}

#[test]
fn settings_that_make_no_sense_are_invalid() {
    for config in [
        json!({ "mode": "ftp" }),
        json!({ "format": "pdf" }),
        json!({ "main_content": "maybe" }),
        json!({ "timeout_ms": 10 }),
        json!({ "timeout_ms": 600_001 }),
        json!({ "timeout_ms": "soon" }),
        json!({ "timeout_ms": [1000] }),
        json!({ "command": "sh 'unclosed" }),
    ] {
        let error = Settings::read(&config).unwrap_err();
        assert_eq!(error.code(), regulus::INVALID_PARAMS, "{config}");
    }
}

#[test]
fn a_command_is_split_into_words_with_quotes() {
    assert_eq!(words("scrapling").unwrap(), ["scrapling"]);
    assert_eq!(
        words("  uvx  --from 'scrapling[shell]' scrapling ").unwrap(),
        ["uvx", "--from", "scrapling[shell]", "scrapling"]
    );
    assert_eq!(
        words(r#""/my tools/scrapling" ''"#).unwrap(),
        ["/my tools/scrapling", ""]
    );
    assert!(words("").unwrap().is_empty());
    assert!(words("a \"b").is_err());
    assert_eq!(settings(json!({ "command": "  " })).command, ["scrapling"]);
}

#[test]
fn only_one_http_url_is_a_url() {
    assert_eq!(
        page_url(&json!("  https://example.com/a \n")).unwrap(),
        "https://example.com/a"
    );
    assert_eq!(
        page_url(&json!("http://127.0.0.1:8000")).unwrap(),
        "http://127.0.0.1:8000"
    );
    for bad in [
        json!("example.com"),
        json!("ftp://example.com"),
        json!("file:///etc/passwd"),
        json!("--proxy=http://evil"),
        json!("https://"),
        json!("https:///path"),
        json!("https://a.com https://b.com"),
        json!("https://a.com\nhttps://b.com"),
        json!(""),
        json!(7),
        json!({ "url": "https://example.com" }),
    ] {
        assert_eq!(
            page_url(&bad).unwrap_err().code(),
            regulus::INVALID_PARAMS,
            "{bad}"
        );
    }
    assert_eq!(
        without_query("https://a.com/p?key=1#top"),
        "https://a.com/p"
    );
    assert_eq!(without_query("https://a.com/p#top"), "https://a.com/p");
}

#[test]
fn the_status_and_the_reason_are_read_from_the_log() {
    let log = "[t] INFO: Fetched (301) <GET http://a>\n[t] INFO: Fetched (200) <GET http://b>\n";
    assert_eq!(status_of(log), Some(200));
    assert_eq!(status_of("Fetched (404) <GET x>"), Some(404));
    assert_eq!(status_of("nothing fetched"), None);
    assert_eq!(status_of("Fetched (20"), None);
    assert_eq!(status_of("Fetched (2000)"), None);
    assert_eq!(status_of("Fetched (é0)"), None);

    let traceback = "[t] ERROR: Failed after 3 attempts: no route\nTraceback (most recent call last):\n  \
                     File \"x.py\", line 1\npatchright._impl._errors.Error: Executable doesn't exist\n\
                     ╔═══╗\n║ <3 Patchright Team ║\n╚═══╝\n";
    assert_eq!(
        error_of(traceback),
        "patchright._impl._errors.Error: Executable doesn't exist"
    );
    assert_eq!(
        error_of("[t] ERROR: Failed after 3 attempts\nUsage: x\n"),
        "Failed after 3 attempts"
    );
    assert_eq!(
        error_of("Usage: scrapling\n\nError: No such option\n"),
        "Error: No such option"
    );
    assert_eq!(error_of(" \n"), "the scrapling command wrote nothing");
    assert_eq!(
        error_of(&"é".repeat(2 * MAX_ERROR_CHARS)).chars().count(),
        MAX_ERROR_CHARS
    );
}

#[test]
fn a_page_goes_to_out_and_the_cli_gets_the_settings() {
    let fake = Fake::new();
    let config = json!({ "command": fake.command(), "format": "text", "selector": "h1" });
    let out = exchange(&[event(7, json!(" https://example.com/ok "), config)]);
    assert_eq!(emitted(&out), ("out".into(), "# Hello\n".into()));
    assert_eq!(out[1]["id"], 7);
    let args = fake.args();
    assert_eq!(
        args[..5],
        [
            "extract",
            "get",
            "--timeout=30",
            "--css-selector=h1",
            "https://example.com/ok"
        ]
    );
    let page = Path::new(&args[5]);
    assert!(args[5].ends_with("page.txt"), "{args:?}");
    assert!(
        !page.parent().unwrap().exists(),
        "the temporary folder is deleted"
    );
}

#[test]
fn a_page_that_fails_goes_to_error_with_the_url_and_the_reason() {
    let fake = Fake::new();
    for (path, config, why) in [
        ("missing", json!({}), "the page answered HTTP 404"),
        ("empty", json!({}), "the page is empty"),
        (
            "empty",
            json!({ "selector": ".x" }),
            "nothing matched the selector",
        ),
        ("nofile", json!({}), "the scrapling command saved no page"),
        (
            "boom",
            json!({}),
            "curl_cffi.curl.CurlError: Failed to connect",
        ),
    ] {
        let mut config = config;
        config["command"] = json!(fake.command());
        let url = format!("https://example.com/{path}?token=secret");
        let out = exchange(&[event(1, json!(url), config)]);
        assert_eq!(
            emitted(&out),
            ("error".into(), format!("https://example.com/{path}: {why}")),
            "{path}"
        );
    }
}

#[test]
fn a_bad_value_or_a_missing_cli_fails_only_its_call() {
    let fake = Fake::new();
    let out = exchange(&[
        event(1, json!("not a url"), json!({})),
        event(
            2,
            json!("https://example.com/ok"),
            json!({ "command": "/nowhere/scrapling" }),
        ),
        event(
            3,
            json!("https://example.com/ok"),
            json!({ "command": fake.command() }),
        ),
    ]);
    assert_eq!(out.len(), 4, "{out:?}");
    assert_eq!(out[0]["error"]["code"], regulus::INVALID_PARAMS);
    assert_eq!(out[1]["error"]["code"], regulus::FAILED);
    let message = out[1]["error"]["message"].as_str().unwrap();
    assert!(
        message.contains("`/nowhere/scrapling` was not found"),
        "{message}"
    );
    assert_eq!(out[2]["params"]["port"], "out");
    assert_eq!(out[3]["id"], 3);
}

#[test]
fn a_cli_that_runs_past_its_deadline_is_killed_with_what_it_started() {
    let fake = Fake::new();
    let command = words(&fake.command()).unwrap();
    let args = ["https://example.com/slow".into(), "/dev/null".into()];
    let started = Instant::now();
    let (status, _log) = run_cli(&command, &args, Duration::from_millis(300)).unwrap();
    assert!(status.is_none(), "it was stopped, it did not exit");
    // The script's `sleep 30` holds the pipes: only killing the whole group returns
    // before DRAIN runs out.
    assert!(started.elapsed() < DRAIN, "{:?}", started.elapsed());
}

#[test]
fn a_home_relative_program_is_expanded() {
    let home = std::env::var_os("HOME").map(PathBuf::from).unwrap();
    assert_eq!(expand_home("~/bin/scrapling"), home.join("bin/scrapling"));
    assert_eq!(expand_home("scrapling"), PathBuf::from("scrapling"));
    assert_eq!(expand_home("~other/x"), PathBuf::from("~other/x"));
}
