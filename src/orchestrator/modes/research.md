---
title: R&D
summary: Research the domain, existing solutions and the current code; find pain points and opportunities.
writes: none        # none | code | tests
web: true           # providers may use web search / fetch
checks: []          # check kinds Foreman runs itself (test, lint, typecheck, build, e2e)
document: research  # saved as .foreman/research/<topic>.md in the project
next: planning
---
## When
The user wants to understand a domain, compare approaches, products or libraries, or find out what users need
before deciding what to build.

## Planner
- Split the research into at most 3 investigate steps with distinct questions (for example: domain and
  competitors, technical options, the current implementation). Put each step in the project closest to it.
- When the topic touches existing code, include the current implementation: what already exists matters as
  much as what others do.
- Web research is allowed. Prefer primary sources (official docs, standards, vendor pages).

## Worker
- Answer your step's question with evidence. Cite a source (URL or file:line) for every claim that matters.
- Separate facts from opinions and say how confident you are.
- Note pain points, risks and opportunities you come across, even slightly outside the question.

## Output
A research brief: Question · Key findings (with sources) · Existing solutions or options compared (pros, cons,
fit for this project) · Pain points and opportunities · Recommendation · Open questions for the user.
