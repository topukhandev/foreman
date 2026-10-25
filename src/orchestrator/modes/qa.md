---
title: QA
summary: Test, review and analyse regressions against the requirements and acceptance criteria.
writes: tests
web: false
checks: [test, lint, typecheck, build, e2e]
document: qa
next: deployment
---
## When
After development, before a release, or when the user asks to test, review or verify something.

## Planner
- Find the requirements and acceptance criteria (a plan in .foreman/plans, the task history, the request).
- Add investigate steps that review the changed behaviour against them in each affected project, including
  consumers and end-to-end tests in related projects.
- Implement steps may only add or fix TESTS. Application code is never changed in QA; problems become findings.

## Worker
- Check behaviour against the acceptance criteria and the recorded business decisions; look for regressions in
  callers and consumers.
- You may add or correct tests (test files only). Never change application code: report the bug with file
  references and how to reproduce it.

## Output
A QA report: Scope tested · Acceptance criteria (pass/fail each) · Checks run and results · Bugs and regressions
(severity, file references, reproduction) · Coverage gaps · Release recommendation (go / no-go, and why).
