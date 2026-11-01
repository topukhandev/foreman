import { describe, expect, it } from "vitest";
import { actions, ago, inbox, mergeTasks, newer, options, searchModels, tone } from "../src/lib/tasks.js";
import { describe as line } from "../src/lib/events.js";

const t = (id, status, extra = {}) => ({ id, status, rev: 1, questions: [], steps: [], ...extra });

describe("tasks", () => {
  it("groups the inbox: what needs the user, what runs, what is planned", () => {
    const g = inbox([t("1", "done"), t("2", "needs_input"), t("3", "running"), t("4", "planned"),
      t("5", "interrupted"), t("6", "done", { running: true }), t("7", "cancelled")]);
    expect(g.needs.map((x) => x.id)).toEqual(["2", "5"]);
    expect(g.running.map((x) => x.id)).toEqual(["3", "6"]);
    expect(g.planned.map((x) => x.id)).toEqual(["4"]);
  });

  it("keeps the snapshot with the higher rev", () => {
    const old = t("1", "running", { rev: 5 });
    const stale = t("1", "queued", { rev: 3 });
    expect(newer(old, stale)).toBe(old);
    expect(mergeTasks([old], [stale, t("2", "done")]).map((x) => [x.id, x.status])).toEqual([["2", "done"], ["1", "running"]]);
  });

  it("offers only what the state allows", () => {
    expect(actions(t("1", "running"))).toMatchObject({ stop: true, resume: false, close: false, answer: false });
    expect(actions(t("1", "interrupted"))).toMatchObject({ stop: false, resume: true, close: true });
    expect(actions(t("1", "planned"))).toMatchObject({ run: true });
    expect(actions(t("1", "done"))).toMatchObject({ resume: false, close: false });
    expect(actions(t("1", "needs_input", { questions: [{ id: "q1" }] }))).toMatchObject({ answer: true, resume: false });
  });

  it("marks the recommended option", () => {
    expect(options({ options: ["Keep it", "Remove it"], descriptions: ["", "drop the dependency"], default: "Remove it" }))
      .toEqual([{ label: "Keep it", description: "", recommended: false }, { label: "Remove it", description: "drop the dependency", recommended: true }]);
  });

  it("describes events and keeps quiet ones quiet", () => {
    expect(line({ event: "step_start", project: "api", step_kind: "implement", provider: "claude" })).toBe("Implement api · claude");
    expect(line({ event: "check", ok: false, project: "api", command: "npm test" })).toBe("✗ Check api: npm test");
    expect(line({ event: "output" })).toBeNull();
    expect(line({ event: "something_new" })).toBeNull();
  });

  it("searches models like the terminal menu: every word a regex", () => {
    const ms = [{ id: "deepseek-v4:free", name: "" }, { id: "deepseek-r2", name: "" }, { id: "openrouter/meta/llama-4-free", name: "Llama" }];
    const ids = (q) => searchModels(ms, q).models.map((m) => m.id);
    expect(ids("deepseek free")).toEqual(["deepseek-v4:free"]);
    expect(ids("DEEPSEEK")).toEqual(["deepseek-v4:free", "deepseek-r2"]);
    expect(ids("(llama|r2)")).toEqual(["deepseek-r2", "openrouter/meta/llama-4-free"]);
    expect(searchModels(ms, "(unclosed")).toEqual({ models: ms, bad: true });
    expect(ids("")).toHaveLength(3);
  });

  it("says when, in words", () => {
    const now = Date.parse("2026-11-01T12:00:00");
    expect(ago("2026-11-01 11:59:30", now)).toBe("just now");
    expect(ago("2026-11-01 11:30:00", now)).toBe("30 min ago");
    expect(ago("2026-11-01 09:00:00", now)).toBe("3 h ago");
    expect(ago("2026-10-31 09:00:00", now)).toBe("yesterday");
    expect(ago("2026-08-01 09:00:00", now)).toBe("2026-08-01");
    expect(ago("garbage", now)).toBe("");
  });

  it("colours a status only as far as it is known", () => {
    expect(tone(t("1", "done"))).toBe("ok");
    expect(tone(t("1", "done", { running: true }))).toBe("info");
    expect(tone(t("1", "failed"))).toBe("bad");
    expect(tone(t("1", "needs_input"))).toBe("warn");
    expect(tone(t("1", "cancelled"))).toBe("");
  });
});
