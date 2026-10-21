# Orchestrator — Initial Plan & Vision

> **Project Codename:** Foreman / Quorum  
> **Goal:** Make life simple for developers and users who have AI providers active on their PC.

---

## The Problem

Modern developers often have multiple AI CLI tools installed (Claude Code, OpenCode, Antigravity, etc.), but using them together is fragmented:

- Each tool has its own interface, workflow, and limitations
- No unified way to delegate, review, and coordinate work across providers
- Manual context-switching kills productivity
- No built-in safety nets (budget guards, review loops, audit trails)

---

## The Vision: One Front Desk, Many Workers

**Orchestrator** is a local, cross-platform AI orchestrator that sits between you and your AI CLIs.

```
You (User)
    │
    ▼
┌─────────────────────────────┐
│  Manager Agent (Front Desk) │  ← Talks to YOU only
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│  Python Core (Controller)   │
│  • Provider Router          │
│  • Budget Guard             │
│  • Hub (File-based Memory)  │
│  • Adapters per CLI         │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│  Worker CLIs (Your PC)      │
│  • antigravity (paid)       │
│  • claude -p (paid)         │
│  • opencode run (free)      │
│  • ...future providers      │
└─────────────────────────────┘
```

### Core Principles

1. **One Front Desk** — You talk to ONE manager. Workers never speak to you directly.
2. **Structured Workflow** — Plan → Critique → Execute → Review → Report.
3. **Independent Review** — Every output reviewed by a different model/session.
4. **Budget Guard** — Hard limits on runs, time, concurrency. Warns at 80%.
5. **Local-First** — Runs on your machine. Your keys, your data, your control.
6. **Zero Config to Start** — Detects installed CLIs, writes `providers.yaml`, works.

---

## Target User Scenarios

| User Type | Pain Point | Orchestrator Value |
|-----------|------------|-------------------|
| **Solo Dev** | Juggling Claude + OpenCode manually | One command runs multi-agent workflows |
| **Team Lead** | No audit trail for AI work | Hub logs every task, decision, review |
| **Cost-Conscious** | Surprise API bills | Budget guard enforces daily limits |
| **Privacy-Focused** | Cloud-only tools | 100% local, no data leaves your PC |
| **Beginner** | "Which AI do I use?" | Setup wizard picks best preset |

---

## MVP Scope (Version 1)

| Component | Status |
|-----------|--------|
| Python core + file hub + budget guard | ✅ Built |
| Provider registry (`providers.yaml`) with auto-detection | ✅ Built |
| Adapters: Antigravity, Claude Code, OpenCode, Mock | ✅ Built |
| Router (role → provider) + fallback logic | ✅ Built |
| Setup wizard + doctor diagnostics | ✅ Built |
| Role packs: Developer, Business Idea, QA | ✅ Built |
| Interactive shell (REPL) | ✅ Built |
| MCP server for GUI apps (Antigravity desktop) | 🔄 In Progress |
| Web dashboard | 📋 Planned |

---

## Roadmap Highlights

- **v0.2**: MCP server stable, more role packs, git worktree isolation
- **v0.3**: Web dashboard, cost tracking per project, plugin system
- **v1.0**: Installer, auto-update, Windows/macOS/Linux packages, docs site

---

## How You Can Help

- Test on your machine: `pip install -e .` → `orchestrator setup`
- Add adapters for your favorite CLI
- Contribute role packs for your workflows
- Report bugs / suggest UX improvements

---

*This document captures the **why** and **what**. For architecture & commands, see `README_ARCHITECTURE.md`.*