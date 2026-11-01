import { describe, expect, it } from "vitest";
import { Rpc, RpcError } from "../src/lib/rpc.js";

function transport() {
  const t = { sent: [], onMsg: null, onClose: null };
  Object.assign(t, {
    start: async () => {},
    send: async (line) => {
      const msg = JSON.parse(line);
      t.sent.push(msg);
      if (msg.method === "system.hello") queueMicrotask(() => t.onMsg(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result: { api_version: "1.2" } })));
    },
    onMessage: async (cb) => (t.onMsg = cb),
    onClosed: async (cb) => (t.onClose = cb),
  });
  return t;
}

describe("Rpc", () => {
  it("says hello first, matches replies by id and passes error kinds on", async () => {
    const t = transport();
    const rpc = new Rpc(t);
    expect((await rpc.connect()).api_version).toBe("1.2");
    expect(t.sent[0].params).toEqual({ client: "desktop/0.1", api_version: "1.4" });
    const a = rpc.call("tasks.get", { task_id: "1" });
    const b = rpc.call("tasks.get", { task_id: "2" });
    const [ida, idb] = t.sent.slice(1).map((m) => m.id);
    t.onMsg(JSON.stringify({ jsonrpc: "2.0", id: idb, error: { code: -32000, message: "No task #2.", data: { kind: "not_found" } } }));
    t.onMsg(JSON.stringify({ jsonrpc: "2.0", id: ida, result: { ok: 1 } }));
    expect(await a).toEqual({ ok: 1 });
    await expect(b).rejects.toMatchObject({ kind: "not_found", message: "No task #2." });
  });

  it("delivers notifications and ignores junk", async () => {
    const t = transport();
    const rpc = new Rpc(t);
    await rpc.connect();
    const seen = [];
    rpc.on("phase", (p) => seen.push(p.text));
    t.onMsg("not json");
    t.onMsg(JSON.stringify({ jsonrpc: "2.0", method: "phase", params: { task_id: "1", text: "Running checks" } }));
    t.onMsg(JSON.stringify({ jsonrpc: "2.0", id: 999, result: {} }));
    expect(seen).toEqual(["Running checks"]);
  });

  it("fails pending calls when the sidecar stops", async () => {
    const t = transport();
    const rpc = new Rpc(t);
    await rpc.connect();
    let down = null;
    rpc.on("disconnected", (info) => (down = info));
    const pending = rpc.call("tasks.list", {});
    t.onClose({ message: "exited", log: "Traceback" });
    await expect(pending).rejects.toBeInstanceOf(RpcError);
    await expect(pending).rejects.toMatchObject({ kind: "disconnected" });
    expect(down.log).toBe("Traceback");
    expect(rpc.connected).toBe(false);
  });
});
