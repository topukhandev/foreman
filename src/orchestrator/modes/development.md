---
title: Development
summary: Implement changes across projects while respecting existing architecture, business rules and dependencies.
writes: code
web: false
checks: [test]
document: null
next: qa
---
## When
The user asks to build, change or fix something and the scope is clear enough to implement.

## Planner
- If a plan for this work exists in .foreman/plans, follow it.
- Assess impact before editing: which consumers, tests and recorded business decisions are affected; rate the risk.
- Keep changes minimal and consistent with the project's conventions.

## Worker
- Follow the project's conventions and the recorded decisions. Keep interfaces consistent with the projects that
  consume them.
- Add or update tests for what you change, and run them.

## Output
What changed per project, which tests ran, and anything left undone.
