# Foreman Orchestrator

> **Local Multi-Agent AI Conductor & Front-Desk Bridge**  
> Coordinate and automate local AI developer CLIs (**Claude Code**, **Antigravity CLI**, **OpenCode**) with deterministic safety guards, isolated sessions, and 4-agent autonomous workflows.

---

## 🌟 Overview

Developers today frequently use multiple AI CLI tools on their local machines. However, using them together is fragmented: each tool operates in isolation, manual context-switching slows development, and there is no unified review loop or budget protection.

**Foreman Orchestrator** solves this by providing a unified, local-first conductor:
- **One Front Desk**: You interact with a single intelligent manager. AI CLI workers execute tasks in the background or in dedicated visual terminal windows.
- **Autonomous Multi-Agent Sprints**: Every complex feature goes through a rigorous **Plan → Critique → Build → Review** cycle with model diversity (Reviewer ≠ Builder).
- **System Resource Guard**: Dynamically governs **10–15 concurrent workers** based on real-time RAM, CPU, and Disk metrics to prevent system lockups.
- **Deterministic Budget Guard**: Enforces daily run quotas, per-task timeouts (300s), and a 3-bounce loop threshold.
- **Zero Config To Start**: Auto-detects installed CLI binaries (across system paths, NVM, FNM, Volta) and creates a machine-local `providers.yaml`.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      User / Front-Desk Manager                  │
│       (Interactive REPL  •  Natural Language  •  MCP Client)   │
└─────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│              Foreman Engine (src/orchestrator/)                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────┐  │
│  │   Router    │  │System Guard │  │Budget Guard │  │  Hub   │  │
│  │(role→prov)  │  │ (RAM & CPU) │  │(Limits/Loop)│  │(Memory)│  │
│  └─────────────┘  └─────────────┘  └─────────────┘  └────────┘  │
└─────────────────────────────────────────────────────────────────┘
                               │
               ┌───────────────┼───────────────┐
               ▼               ▼               ▼
        ┌────────────┐  ┌────────────┐  ┌────────────┐
        │  Adapter:  │  │  Adapter:  │  │  Adapter:  │
        │Antigravity │  │Claude Code │  │  OpenCode  │
        └────────────┘  └────────────┘  └────────────┘
               │               │               │
               ▼               ▼               ▼
        ┌────────────────────────────────────────────┐
        │       Local CLI Tools & Desktop Windows    │
        │   (agy -p  •  claude -p  •  opencode run)  │
        └────────────────────────────────────────────┘
```

---

## 🚀 Installation & Quick Start

### 1. Requirements
- Linux or macOS (Python 3.9+)
- At least one supported AI CLI installed:
  - **Antigravity CLI (`agy`)**
  - **Claude Code (`claude`)**
  - **OpenCode (`opencode` / `opencode-cli`)**

### 2. Install
```bash
git clone https://github.com/your-username/Orcastrator.git
cd Orcastrator
pip install -e .
```

### 3. Launch Interactive Shell
```bash
orchestrator
```
*(Running `orchestrator` with no arguments automatically initializes the project hub and launches the interactive REPL).*

---

## 💬 Interactive REPL & Slash Commands

Inside the Foreman REPL, you can chat in natural language with your active AI worker or trigger specialized commands using `/`:

| Command | Description |
| :--- | :--- |
| `/run <goal>` | Launch a full 4-agent autonomous sprint on a goal |
| `/provider` | Switch active default AI worker (Antigravity, Claude, OpenCode, Dual-Model) |
| `/visual` | Toggle visible desktop terminal windows (on/off) |
| `/status` | View the single-page project state dashboard (`hub/state.md`) |
| `/system` | Inspect real-time System Resource Guard metrics (RAM, CPU, Disk headroom) |
| `/tasks` | Inspect task specs, checklists, and reviewer verdicts |
| `/new` | Start a fresh conversation session with the active AI worker |
| `/link <path>` | Connect an external repository, directory, or microservice |
| `/doctor` | Run setup diagnostics on installed AI CLIs |
| `/clear` | Clear the terminal screen |
| `/help` | Display interactive command guide |
| `/exit` | Exit the orchestrator session |

---

## ⚡ 4-Agent Autonomous Sprints

When you run `/run <goal>` (or `orchestrator run "<goal>"`), Foreman executes a structured workflow loop:

1. **Plan (Planner)**: Decomposes the goal into an actionable task checklist with scope, risks, and criteria.
2. **Critique (Critic)**: An adversarial review session that challenges assumptions and finds missing edge cases.
3. **Execute (Builder)**: Dispatches implementation tasks to worker sessions inside the target project directory.
4. **Independent Review (Reviewer)**: Verified and audited by a clean session or different model/provider (**Reviewer ≠ Builder** strictly enforced).
5. **Report**: Aggregates the results into a concise executive summary written to `hub/state.md`.

---

## 🖥️ CLI Commands Reference

You can also use Foreman directly from your system shell without entering the REPL:

```bash
# Setup & Health Check
orchestrator setup              # Interactive provider setup wizard
orchestrator doctor             # Diagnose installed CLI binaries & paths
orchestrator status             # Print current hub state & active tasks

# Run Workflows
orchestrator run "Build auth"   # Run developer workflow pack
orchestrator run "Market MVP"   # Run business idea analysis pack
orchestrator run "Unit tests"   # Run QA & test generation pack
orchestrator run "Goal" --mock  # Simulation mode (zero tokens consumed)
orchestrator run "Goal" --visual # Force visible desktop terminal windows

# Model Context Protocol (MCP)
orchestrator mcp                # Start stdio MCP server for IDEs / Antigravity Desktop
```

---

## 🛡️ Budget & System Resource Guard

- **Dynamic Concurrency**: Up to 10–15 concurrent workers globally (default: 10), actively monitored by the System Resource Guard (RAM & CPU checks).
- **Daily Quotas**: Tracked per provider in `providers.yaml`; warns at 80% and halts at 100%.
- **Task Timeout**: 300 seconds default.
- **Anti-Loop Threshold**: Maximum 3 review bounces before halting execution and escalating to the manager as a blocker.

---

## 📂 File Hub Layout

Foreman uses a lightweight, transparent file-based hub inside your project:

```text
my-project/
├── providers.yaml          # Local provider registry & limits (never committed)
└── hub/
    ├── state.md            # Single-page executive summary & active tasks
    ├── tasks/              # Individual task markdown checklists & specs
    ├── inbox/              # JSON message protocol (assign, result, review)
    └── logs/               # Raw execution logs for debugging
```

---

## 🧪 Running Tests

To run the complete test suite:

```bash
python3 -m unittest discover tests
```

---

## 📄 License

Open source under the MIT License.
