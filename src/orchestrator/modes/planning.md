---
title: Planning
summary: Analyse the current state and requirements, assess impact, and produce an actionable plan. Never assumes a blank slate.
writes: none
web: true
checks: []
document: plans
next: development
---
## When
The user asks for a plan or design; or a requested change is large, ambiguous or risky enough that building it
without a plan would likely go wrong; or an existing feature with several consumers is about to be modified.

## Planner
- Start from what exists. Add an investigate step that maps the current implementation of the feature (entry
  points, data model, business rules, tests) in every project it lives in, before anything is planned.
- Add impact investigation for consumers and tests in related projects when interfaces may change.
- Business rules that cannot be inferred from code, docs or recorded decisions become questions; never invent them.

## Worker
- Describe the current implementation precisely, with file references, before proposing changes.
- Identify what must change, what must stay compatible, and what could regress.

## Output
An implementation plan: Goal · Current state (what exists, with file references) · Proposed changes per project
(in order, naming the interfaces that must stay consistent) · Impact and regression risks · Tests to add or
update · Acceptance criteria · Open business questions · Rollout notes.
