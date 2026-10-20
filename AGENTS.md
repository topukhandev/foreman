# AGENTS.md — Manager & Worker Operational Rules

This document specifies the operational rules for the Front-Desk Manager and Worker agents within the Foreman / Quorum Orchestrator.

---

## 1. Front-Desk Manager Rules

### Principle 1: One Front Desk
- The user interacts **only** with the Manager agent.
- AI CLI workers never communicate with the user directly.
- The manager aggregates results and delivers **concise, high-signal final reports**.

### Principle 2: Memory & Context Economy
- The manager reads **only `hub/state.md`** by default.
- Never load all files or raw logs into the manager context.
- Open individual `hub/tasks/<id>.md` files only when investigating a specific task or blocker.
- Maintain `hub/state.md` at **one single page** maximum.

### Principle 3: Structured Workflow Loop
When the user specifies a goal:
1. **Plan**: Formulate a complete task checklist via a planner worker (covering scope, risks, data sources, legal/financial costs).
2. **Critique**: Submit the plan to an adversarial critic session to expose blind spots and missing assumptions.
3. **Execute**: Dispatch tasks to worker sessions (`agy -p`, `claude -p`, etc.).
4. **Independent Review**: Every output must be reviewed and approved by a different model/provider, or an isolated clean session.
5. **Report**: Give the user a short, readable executive summary. Never overwhelm the user with raw output.

### Principle 4: User Interruption Threshold
Interrupt the user **only** when:
- A blocker requires user manual intervention (e.g. `claude login`, API key configuration, paid tier approval).
- A genuine strategic decision or trade-off requires human choice.
- Budget Guard issues an 80% daily quota warning requiring user sign-off to proceed.

---

## 2. Worker Agent Rules

1. **Scoped Execution**: Workers operate strictly inside the target project directory.
2. **Standard Message Protocol**:
   - `assign`: Received from manager with prompt, context, and role.
   - `result`: Emitted to manager upon task execution.
   - `review`: Evaluated by independent reviewer against acceptance criteria.
   - `revise`: Re-routed back to worker if reviewer rejects output (subject to loop limit).
3. **No Direct User Prompts**: Workers must never ask the user questions in stdin; they return structured outputs or flags to the hub.

---

## 3. Budget Guard Enforcements (Deterministic Python)
- **Daily Runs**: Tracked per provider; hard cutoff at 100% with warning at 80%.
- **Concurrency**: Maximum 2 concurrent workers globally.
- **Task Timeout**: 300 seconds default.
- **Loop Threshold**: Maximum 3 review bounces before halting execution and escalating to manager as a blocker.
