//! The `foreman serve --stdio` process (docs/api-contract.md): started by the page (rpc_start), fed checked request
//! lines (rpc_send), and read line by line. Responses and notifications go to the page as `rpc://message` events;
//! responses to the shell's own requests (ids "native:N", used for confirmations) stay here. When the process ends,
//! the page gets `rpc://closed` with the last lines Foreman wrote to stderr, so it can say why.
//!
//! Tasks don't run in this process: Foreman starts a detached runner per task, so they outlive the app.

use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};

const LOG_LINES: usize = 200;

struct Proc {
    child: Child,
    stdin: ChildStdin,
}

#[derive(Default)]
pub struct Sidecar {
    proc: Mutex<Option<Proc>>,
    waiting: Mutex<HashMap<String, mpsc::Sender<Value>>>,
    next: AtomicU64,
    generation: AtomicU64,
    log: Arc<Mutex<VecDeque<String>>>,
    /// The app is quitting: the window closes without asking.
    pub exiting: AtomicBool,
    /// When a close was last requested and the page hasn't acknowledged it yet (see main.rs).
    pub close_asked: Mutex<Option<Instant>>,
}

fn foreman_command() -> Result<(Command, String), String> {
    #[cfg(debug_assertions)]
    if let Some(bin) = std::env::var_os("FOREMAN_BIN") {
        // development builds only: run Foreman from a checkout
        let shown = bin.to_string_lossy().into_owned();
        return Ok((Command::new(bin), shown));
    }
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")).map(Into::into);
    let path = crate::policy::find_foreman(std::env::var_os("PATH"), home).ok_or(
        "couldn't find the `foreman` command. Install Foreman (`pip install -e .` in its folder) so that `foreman` \
         is on your PATH or in ~/.local/bin, then start again.",
    )?;
    let shown = path.display().to_string();
    Ok((Command::new(path), shown))
}

impl Sidecar {
    /// Start Foreman unless it is already running.
    pub fn start(&self, app: &AppHandle) -> Result<(), String> {
        let mut guard = self.proc.lock().unwrap();
        if let Some(p) = guard.as_mut() {
            if matches!(p.child.try_wait(), Ok(None)) {
                return Ok(());
            }
        }
        *guard = None;
        let generation = self.generation.fetch_add(1, Ordering::SeqCst) + 1;
        let (mut cmd, shown) = foreman_command()?;
        cmd.args(["serve", "--stdio"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .env("PYTHONIOENCODING", "utf-8");
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
        }
        let mut child = cmd.spawn().map_err(|e| format!("couldn't start {shown}: {e}"))?;
        let stdin = child.stdin.take().ok_or("no stdin")?;
        let stdout = child.stdout.take().ok_or("no stdout")?;
        let stderr = child.stderr.take().ok_or("no stderr")?;

        let log = self.log.clone();
        log.lock().unwrap().clear();
        thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                #[cfg(debug_assertions)]
                eprintln!("[foreman] {line}");
                let mut log = log.lock().unwrap();
                if log.len() >= LOG_LINES {
                    log.pop_front();
                }
                log.push_back(line);
            }
        });

        let handle = app.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut buf = Vec::new();
            loop {
                buf.clear();
                match reader.read_until(b'\n', &mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {}
                }
                let line = String::from_utf8_lossy(&buf).trim_end().to_string();
                if line.is_empty() {
                    continue;
                }
                let state = handle.state::<Sidecar>();
                if !state.deliver_own(&line) {
                    let _ = handle.emit("rpc://message", line);
                }
            }
            handle.state::<Sidecar>().ended(&handle, generation);
        });

        *guard = Some(Proc { child, stdin });
        Ok(())
    }

    /// A response to one of the shell's own requests: handed to whoever waits for it.
    fn deliver_own(&self, line: &str) -> bool {
        if !line.contains("native:") {
            return false;
        }
        let Ok(msg) = serde_json::from_str::<Value>(line) else { return false };
        let Some(id) = msg.get("id").and_then(Value::as_str).filter(|id| id.starts_with("native:")) else {
            return false;
        };
        if let Some(tx) = self.waiting.lock().unwrap().remove(id) {
            let _ = tx.send(msg);
        }
        true
    }

    fn ended(&self, app: &AppHandle, generation: u64) {
        if generation != self.generation.load(Ordering::SeqCst) {
            return; // an earlier process, replaced or shut down on purpose
        }
        let code = self.proc.lock().unwrap().take().and_then(|mut p| p.child.wait().ok()).and_then(|s| s.code());
        self.waiting.lock().unwrap().clear();
        let log: Vec<String> = self.log.lock().unwrap().iter().rev().take(40).rev().cloned().collect();
        let message = match code {
            Some(0) => "Foreman stopped.".to_string(),
            Some(c) => format!("Foreman stopped (exit code {c})."),
            None => "Foreman stopped.".to_string(),
        };
        let _ = app.emit("rpc://closed", json!({"message": message, "log": log.join("\n")}));
    }

    pub fn send_line(&self, line: &str) -> Result<(), String> {
        let mut guard = self.proc.lock().unwrap();
        let proc = guard.as_mut().ok_or("Foreman isn't running")?;
        proc.stdin
            .write_all(format!("{line}\n").as_bytes())
            .and_then(|_| proc.stdin.flush())
            .map_err(|e| format!("couldn't reach Foreman: {e}"))
    }

    /// A request of the shell's own, waiting up to `timeout` for the whole response object.
    pub fn request(&self, method: &str, params: Value, timeout: Duration) -> Result<Value, String> {
        let id = format!("native:{}", self.next.fetch_add(1, Ordering::SeqCst) + 1);
        let (tx, rx) = mpsc::channel();
        self.waiting.lock().unwrap().insert(id.clone(), tx);
        let sent = self.send_line(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}).to_string());
        let result = sent.and_then(|_| rx.recv_timeout(timeout).map_err(|_| format!("{method}: no answer from Foreman")));
        self.waiting.lock().unwrap().remove(&id);
        result
    }

    /// Close Foreman's input (it ends when stdin closes), wait briefly, then make sure it is gone.
    pub fn shutdown(&self) {
        self.generation.fetch_add(1, Ordering::SeqCst);
        let Some(Proc { mut child, stdin }) = self.proc.lock().unwrap().take() else { return };
        drop(stdin);
        let until = Instant::now() + Duration::from_millis(1500);
        while Instant::now() < until {
            if matches!(child.try_wait(), Ok(Some(_))) {
                return;
            }
            thread::sleep(Duration::from_millis(50));
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}
