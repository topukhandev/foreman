//! What the shell allows, as plain functions (no Tauri types, so they are unit-tested on their own):
//! which JSON-RPC requests the webview may send, which answers need a native confirmation first and what it says,
//! which links may open, and where the `foreman` command is.

use serde_json::{Map, Value};
use std::ffi::OsString;
use std::path::{Path, PathBuf};

/// The API methods the app uses (docs/api-contract.md). Anything else from the webview is refused here, before it
/// reaches Foreman.
pub const METHODS: &[&str] = &[
    "system.hello", "system.doctor", "system.stats",
    "tasks.list", "tasks.get", "tasks.submit", "tasks.answer", "tasks.resume", "tasks.next", "tasks.stop",
    "tasks.close", "tasks.events", "tasks.outputs", "tasks.output",
    "projects.list", "projects.get", "projects.scan", "projects.open", "projects.hide", "lifecycle.get", "sessions.list",
    "suggestions.list", "suggestions.accept", "suggestions.dismiss",
    "config.get", "config.set", "config.set_on_close", "models.list", "models.set",
    "conversations.list", "conversations.get", "conversations.send", "conversations.rename",
    "events.subscribe", "events.unsubscribe",
];

/// Question kinds answered only after a native confirmation built from the task record (high-impact plan
/// approvals and new dependencies). The page can't answer them on its own, even if its content were compromised.
pub const CONFIRMED_KINDS: &[&str] = &["approval", "dependency"];

/// A request line from the webview, checked: one JSON-RPC 2.0 request with a numeric id, an allowed method and
/// object params. Returns it re-serialised (so exactly one line goes to Foreman).
pub fn check_request(line: &str) -> Result<Value, String> {
    let msg: Value = serde_json::from_str(line).map_err(|_| "not JSON".to_string())?;
    let obj = msg.as_object().ok_or("not a JSON object")?;
    if obj.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return Err("not JSON-RPC 2.0".into());
    }
    if !obj.get("id").is_some_and(Value::is_u64) {
        return Err("requests need a numeric id".into()); // string ids are the shell's own ("native:…")
    }
    let method = obj.get("method").and_then(Value::as_str).ok_or("no method")?;
    if !METHODS.contains(&method) {
        return Err(format!("method {method} is not available to the app"));
    }
    match obj.get("params") {
        None | Some(Value::Object(_)) => {}
        Some(_) => return Err("params must be an object".into()),
    }
    if obj.keys().any(|k| !matches!(k.as_str(), "jsonrpc" | "id" | "method" | "params")) {
        return Err("unexpected fields in the request".into());
    }
    Ok(msg)
}

/// Text as it may appear in a native dialog: no control characters or direction overrides, and not too long.
pub fn clean(text: &str, limit: usize) -> String {
    let mut out: String = text
        .chars()
        .filter(|c| {
            (!c.is_control() || *c == '\n')
                && !matches!(*c, '\u{200e}' | '\u{200f}' | '\u{061c}' | '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        })
        .collect();
    if out.chars().count() > limit {
        out = out.chars().take(limit).collect::<String>() + "…";
    }
    out
}

fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

/// The confirmation for a `tasks.answer` request, or None when none of its answers needs one. Built from the task
/// record's deterministic data: the user's own request, the plan's steps and projects, the impact rating, the
/// dependency names, and the answer being sent. The AI-written question text and title are left out.
pub fn confirmation(record: &Value, answers: &Map<String, Value>) -> Option<String> {
    let questions = record.get("questions")?.as_array()?;
    let picked: Vec<&Value> = questions
        .iter()
        .filter(|q| {
            let id = q.get("id").and_then(Value::as_str).unwrap_or("");
            let open = q.get("answer").map_or(true, Value::is_null);
            let kind = q.get("kind").and_then(Value::as_str).unwrap_or("");
            open && answers.contains_key(id) && CONFIRMED_KINDS.contains(&kind)
        })
        .collect();
    if picked.is_empty() {
        return None;
    }
    let mut out = format!(
        "Task #{}\nYour request: {}\n",
        clean(&text(&record["id"]), 20),
        clean(&text(&record["request"]), 300)
    );
    for q in picked {
        let id = q["id"].as_str().unwrap_or("");
        let answer = clean(&text(&answers[id]), 200);
        if q["kind"] == "approval" {
            let impact = &record["impact"];
            out += &format!("\nApproval of the plan (impact: {} risk)\n", clean(&text(&impact["risk"]), 20));
            let steps: Vec<String> = record["steps"]
                .as_array()
                .map(|s| {
                    s.iter()
                        .filter(|s| !matches!(s["status"].as_str(), Some("done") | Some("skipped")))
                        .take(12)
                        .map(|s| format!("  • {} in {}", clean(&text(&s["kind"]), 30), clean(&text(&s["project"]), 60)))
                        .collect()
                })
                .unwrap_or_default();
            if !steps.is_empty() {
                out += &format!("Steps to run:\n{}\n", steps.join("\n"));
            }
            if let Some(consumers) = impact["consumers"].as_array().filter(|c| !c.is_empty()) {
                let names: Vec<String> = consumers.iter().map(|c| clean(&text(c), 60)).collect();
                out += &format!("Used by: {}\n", names.join(", "));
            }
        } else {
            let deps: Vec<String> = q["deps"]
                .as_array()
                .map(|d| d.iter().take(12).map(|d| clean(&text(d), 80)).collect())
                .unwrap_or_default();
            out += &format!(
                "\nNew dependency in {}: {}\n",
                clean(&text(&q["project"]), 60),
                if deps.is_empty() { "(see the task)".to_string() } else { deps.join(", ") }
            );
        }
        out += &format!("Your answer: {answer}\n");
    }
    Some(out)
}

/// An http(s) link that may be opened in the browser, or None.
pub fn safe_link(url: &str) -> Option<String> {
    let url = url.trim();
    let lower = url.to_ascii_lowercase();
    let rest = lower.strip_prefix("https://").or_else(|| lower.strip_prefix("http://"))?;
    if rest.is_empty() || rest.starts_with('/') || url.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return None;
    }
    Some(url.to_string())
}

/// Where to look for `foreman`: PATH, then the usual install folders (a desktop app may not get the shell's PATH).
pub fn find_foreman(path: Option<OsString>, home: Option<PathBuf>) -> Option<PathBuf> {
    let names: &[&str] = if cfg!(windows) { &["foreman.exe"] } else { &["foreman"] };
    let mut dirs: Vec<PathBuf> = path.map(|p| std::env::split_paths(&p).collect()).unwrap_or_default();
    if let Some(home) = home {
        dirs.push(home.join(".local").join("bin"));
    }
    if !cfg!(windows) {
        dirs.push(PathBuf::from("/opt/homebrew/bin"));
        dirs.push(PathBuf::from("/usr/local/bin"));
    }
    dirs.iter().flat_map(|d| names.iter().map(move |n| d.join(n))).find(|p| runnable(p))
}

fn runnable(path: &Path) -> bool {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        path.metadata().map(|m| m.is_file() && m.permissions().mode() & 0o111 != 0).unwrap_or(false)
    }
    #[cfg(not(unix))]
    {
        path.is_file()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn requests_are_checked() {
        assert!(check_request(r#"{"jsonrpc":"2.0","id":1,"method":"tasks.list","params":{}}"#).is_ok());
        assert!(check_request(r#"{"jsonrpc":"2.0","id":2,"method":"tasks.get"}"#).is_ok());
        for bad in [
            "nope",
            "[]",
            r#"{"jsonrpc":"1.0","id":1,"method":"tasks.list"}"#,
            r#"{"jsonrpc":"2.0","method":"tasks.list"}"#,
            r#"{"jsonrpc":"2.0","id":"native:1","method":"tasks.list"}"#,
            r#"{"jsonrpc":"2.0","id":1,"method":"run.command"}"#,
            r#"{"jsonrpc":"2.0","id":1,"method":"tasks.list","params":[1]}"#,
            r#"{"jsonrpc":"2.0","id":1,"method":"tasks.list","extra":1}"#,
        ] {
            assert!(check_request(bad).is_err(), "{bad}");
        }
    }

    fn record() -> Value {
        json!({
            "id": "7", "request": "Change refunds\u{202e}gnp.exe",
            "impact": {"risk": "high", "consumers": ["admin-ui"]},
            "steps": [{"kind": "implement", "project": "orders-api", "status": "pending"},
                      {"kind": "investigate", "project": "orders-api", "status": "done"}],
            "questions": [
                {"id": "q1", "kind": "business", "text": "Which colour?", "answer": null},
                {"id": "q2", "kind": "approval", "text": "AI text that is not shown", "answer": null},
                {"id": "q3", "kind": "dependency", "deps": ["left-pad"], "project": "admin-ui", "answer": null},
                {"id": "q4", "kind": "approval", "answer": "yes"}
            ]
        })
    }

    #[test]
    fn approvals_and_dependencies_are_confirmed_from_the_record() {
        let answers = json!({"q2": "yes", "q3": "Keep it"});
        let text = confirmation(&record(), answers.as_object().unwrap()).expect("needs a confirmation");
        assert!(text.contains("Task #7"));
        assert!(text.contains("Change refundsgnp.exe"), "no direction override: {text}");
        assert!(text.contains("impact: high risk"));
        assert!(text.contains("• implement in orders-api"));
        assert!(!text.contains("investigate"), "done steps are not listed");
        assert!(text.contains("Used by: admin-ui"));
        assert!(text.contains("New dependency in admin-ui: left-pad"));
        assert!(text.contains("Your answer: yes") && text.contains("Your answer: Keep it"));
        assert!(!text.contains("AI text"));
    }

    #[test]
    fn other_answers_need_no_confirmation() {
        let answers = json!({"q1": "blue", "q4": "yes", "q9": "x"});
        assert_eq!(confirmation(&record(), answers.as_object().unwrap()), None);
    }

    #[test]
    fn only_http_links_open() {
        assert_eq!(safe_link(" https://example.com/a "), Some("https://example.com/a".into()));
        assert_eq!(safe_link("HTTP://example.com"), Some("HTTP://example.com".into()));
        for bad in ["javascript:alert(1)", "file:///etc/passwd", "https://", "https:///x", "http://a b", "ftp://x", "//x"] {
            assert_eq!(safe_link(bad), None, "{bad}");
        }
    }

    #[test]
    fn foreman_is_found_on_path_or_in_local_bin() {
        let dir = std::env::temp_dir().join(format!("foreman-find-{}", std::process::id()));
        let bin = dir.join(".local").join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let name = if cfg!(windows) { "foreman.exe" } else { "foreman" };
        std::fs::write(bin.join(name), "").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(bin.join(name), std::fs::Permissions::from_mode(0o755)).unwrap();
        }
        assert_eq!(find_foreman(None, Some(dir.clone())), Some(bin.join(name)));
        assert_eq!(find_foreman(Some(bin.clone().into_os_string()), None), Some(bin.join(name)));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dialog_text_is_cleaned_and_capped() {
        assert_eq!(clean("a\u{1b}[31mb\u{2066}c", 10), "a[31mbc");
        assert_eq!(clean("abcdef", 3), "abc…");
    }
}
