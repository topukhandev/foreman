// JSON-RPC 2.0 client for `foreman serve --stdio` (docs/api-contract.md). The transport moves lines: in the app it
// is the Tauri shell (src-tauri relays the sidecar's stdin/stdout); in a plain browser it is the demo (fake.js).
//
// transport = { start(): Promise, send(line): Promise, onMessage(cb): Promise<unlisten>, onClosed(cb): Promise<unlisten> }

export const API_VERSION = "1.4";
export const CLIENT = "desktop/0.1";

export class RpcError extends Error {
  constructor(message, kind = "internal", data = {}) {
    super(message);
    this.kind = kind; // not_found | ambiguous | running | stale | invalid | internal | cancelled | disconnected | timeout
    this.data = data;
  }
}

export class Rpc {
  constructor(transport) {
    this.transport = transport;
    this.pending = new Map();
    this.nextId = 1;
    this.listeners = new Map(); // notification method -> Set(callback)
    this.connected = false;
  }

  /** Attach to the transport once; then start (or restart) the sidecar and say hello. */
  async connect() {
    if (!this.attached) {
      this.attached = true;
      await this.transport.onMessage((line) => this.receive(line));
      await this.transport.onClosed((info) => this.closed(info));
    }
    await this.transport.start();
    const hello = await this.call("system.hello", { client: CLIENT, api_version: API_VERSION });
    this.connected = true;
    this.emit("connected", hello);
    return hello;
  }

  on(method, callback) {
    if (!this.listeners.has(method)) this.listeners.set(method, new Set());
    this.listeners.get(method).add(callback);
    return () => this.listeners.get(method).delete(callback);
  }

  emit(method, params) {
    for (const cb of this.listeners.get(method) || []) {
      try {
        cb(params);
      } catch (e) {
        console.error(`listener for ${method}:`, e);
      }
    }
  }

  call(method, params = {}, timeoutMs = 30000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RpcError(`${method} took too long`, "timeout"));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      this.transport.send(JSON.stringify({ jsonrpc: "2.0", id, method, params })).catch((e) => {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new RpcError(String(e?.message ?? e), "disconnected"));
      });
    });
  }

  receive(line) {
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      return;
    }
    if (msg && msg.id != null && this.pending.has(msg.id)) {
      const { resolve, reject, timer } = this.pending.get(msg.id);
      clearTimeout(timer);
      this.pending.delete(msg.id);
      if (msg.error) {
        const data = msg.error.data || {};
        reject(new RpcError(msg.error.message, data.kind || "internal", data));
      } else {
        resolve(msg.result);
      }
    } else if (msg && typeof msg.method === "string" && msg.id == null) {
      this.emit(msg.method, msg.params || {});
    }
  }

  closed(info) {
    this.connected = false;
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new RpcError("Foreman stopped", "disconnected"));
    }
    this.pending.clear();
    this.emit("disconnected", info || {});
  }
}

/** The Tauri shell's transport (src-tauri/src/sidecar.rs). Loaded only inside the app. */
export async function tauriTransport() {
  const { invoke } = await import("@tauri-apps/api/core");
  const { listen } = await import("@tauri-apps/api/event");
  return {
    start: () => invoke("rpc_start"),
    send: (line) => invoke("rpc_send", { line }),
    onMessage: (cb) => listen("rpc://message", (e) => cb(e.payload)),
    onClosed: (cb) => listen("rpc://closed", (e) => cb(e.payload)),
  };
}

export const inTauri = () => typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

/** End-to-end tests outside Tauri: a test harness that relays lines to a real `foreman serve --stdio` sets
 * window.__foremanBridge = {send(line), onLine(cb), onClose(cb)} before the page loads. Never set in the app. */
export function bridgeTransport() {
  const b = typeof window !== "undefined" && !inTauri() ? window.__foremanBridge : null;
  if (!b) return null;
  return {
    start: async () => {},
    send: async (line) => b.send(line),
    onMessage: async (cb) => b.onLine(cb),
    onClosed: async (cb) => b.onClose(cb),
  };
}
