---
title: Deployment
summary: Prepare and verify a release (config and migration checks, checklist, rollback plan, post-deploy verification). Never deploys.
writes: none
web: false
checks: [test, build, lint]
document: releases
next: null
---
## When
The user wants to ship, prepare a release, check deployment readiness, plan a rollback, or verify a deployment
they performed.

## Planner
- Foreman never deploys and never runs deploy, publish, migration or infrastructure commands. The user deploys.
- Investigate: pending migrations, new environment variables or config keys, feature flags, dependency changes,
  build output, version and changelog, and the required deploy order between projects.
- To verify a deployment the user performed, plan read-only checks and give verification commands for the user.

## Worker
- Read-only. Report what must happen before, during and after the deploy, with file references.
- Write the exact commands the user should run, but never run them.

## Output
A release document: What is released (per project) · Pre-deploy checklist · Required config/env and migrations ·
Deploy order across projects · Rollback plan · Post-deploy verification (commands for the user) · Risks.
