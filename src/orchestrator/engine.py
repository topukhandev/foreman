"""Main Orchestrator Engine: Coordinates worker lifecycle, review loop, and hub state."""

from dataclasses import dataclass
import os
import time
from typing import Callable, Dict, List, Optional
from orchestrator.config import OrchestratorConfig
from orchestrator.budget import BudgetGuard, BudgetExceededError, LoopDetectedError
from orchestrator.hub import Hub, InboxMessage
from orchestrator.router import Router
from orchestrator.progress import ProgressTracker
from orchestrator.adapters import create_adapter, BaseCLIAdapter
from orchestrator.packs import PACKS, PackTaskSpec


@dataclass
class OrchestratorRunResult:
    success: bool
    goal: str
    elapsed_seconds: float
    tasks_completed: int
    tasks_failed: int
    summary_report: str
    blockers: List[str]


class OrchestratorEngine:
    """Core execution engine managing tasks, workers, reviews, and progress."""

    def __init__(self, project_dir: str, config: OrchestratorConfig, status_callback: Optional[Callable[[str], None]] = None):
        self.project_dir = os.path.abspath(project_dir)
        self.config = config
        self.status_callback = status_callback

        # Initialize Hub in project directory
        self.hub = Hub(self.project_dir)

        # Initialize Budget Guard
        ledger_path = os.path.join(self.project_dir, "hub", ".budget_ledger.json")
        self.budget = BudgetGuard(config.budget, config.providers, ledger_path=ledger_path)

        # Initialize Router
        self.router = Router(config.providers, config.routing)

        # Adapters cache
        self.adapters: Dict[str, BaseCLIAdapter] = {}
        for p_name, p_cfg in config.providers.items():
            self.adapters[p_name] = create_adapter(
                p_name,
                p_cfg,
                timeout_sec=config.budget.max_time_per_task_sec
            )

        # Progress tracker
        self.progress = ProgressTracker()
        self.blockers: List[str] = []

    def _notify_progress(self) -> None:
        """Render status and trigger status callback."""
        dashboard = self.progress.render()
        if self.status_callback:
            self.status_callback(dashboard)

        # Synchronize hub/state.md
        active_items = []
        for agent in self.progress.active_agents.values():
            active_items.append({
                "id": agent.task_id,
                "title": agent.task_title,
                "role": agent.role,
                "worker": agent.worker_provider,
                "running_sec": agent.running_seconds,
                "status": "running"
            })

        completed_summaries = [
            f"Task #{t.task_id} ({t.title}): {t.summary or 'Done'}"
            for t in self.progress.tasks.values() if t.status == "done"
        ]

        self.hub.write_state(
            goal=self.progress.goal,
            phase="active" if active_items else "idle",
            active_tasks=active_items,
            blockers=self.blockers,
            completed_tasks=completed_summaries
        )

    def run_pack(self, pack_name: str, goal: str) -> OrchestratorRunResult:
        """Run a standard role pack workflow (e.g. business_idea, developer, qa)."""
        if pack_name in PACKS:
            pack_cls = PACKS[pack_name]
            tasks = pack_cls.get_tasks(goal)
            return self.run_tasks(goal=goal, tasks=tasks)
        else:
            available = ", ".join(PACKS.keys())
            raise ValueError(f"Unknown pack '{pack_name}'. Available packs: {available}")

    def run_tasks(self, goal: str, tasks: List[PackTaskSpec]) -> OrchestratorRunResult:
        """Execute a sequence of tasks with planner -> executor -> reviewer loop."""
        start_time = time.time()
        self.progress.start_orchestrator(goal)
        self.blockers.clear()

        # Check project size limits first
        ok, dir_err = self.budget.check_project_directory(self.project_dir)
        if not ok:
            self.blockers.append(dir_err or "Project directory limit exceeded")
            self._notify_progress()
            return OrchestratorRunResult(
                success=False,
                goal=goal,
                elapsed_seconds=time.time() - start_time,
                tasks_completed=0,
                tasks_failed=len(tasks),
                summary_report=f"Execution blocked: {dir_err}",
                blockers=self.blockers
            )

        # Register all tasks in progress tracker
        for t in tasks:
            self.progress.register_task(t.task_id, t.title, t.role)
        self._notify_progress()

        previous_output = ""
        completed_count = 0
        failed_count = 0

        for task_spec in tasks:
            task_id = task_spec.task_id
            title = task_spec.title
            role = task_spec.role

            # Format prompt with context
            prompt = task_spec.prompt_template.format(
                goal=goal,
                previous_output=previous_output or "None (initial step)"
            )

            # Record task in Hub
            self.hub.save_task_spec(
                task_id=task_id,
                title=title,
                prompt=prompt,
                role=role,
                context=previous_output
            )

            # Select worker provider
            worker_info = self.router.select_worker(role)
            if not worker_info:
                blocker_msg = f"No available provider found for role '{role}'"
                self.blockers.append(blocker_msg)
                self.progress.set_task_status(task_id, "blocked", blocker_msg)
                self._notify_progress()
                failed_count += 1
                break

            worker_provider, session_id = worker_info

            # Budget check
            try:
                allowed, warn_msg = self.budget.check_and_acquire(task_id, worker_provider)
                if not allowed:
                    self.blockers.append(warn_msg or "Budget check failed")
                    self.progress.set_task_status(task_id, "blocked", warn_msg or "Budget blocked")
                    self._notify_progress()
                    failed_count += 1
                    break
                if warn_msg:
                    self.blockers.append(warn_msg)
            except (BudgetExceededError, LoopDetectedError) as e:
                self.blockers.append(str(e))
                self.progress.set_task_status(task_id, "blocked", str(e))
                self._notify_progress()
                failed_count += 1
                break

            # Send assign message to inbox
            self.hub.send_message(InboxMessage(
                msg_id=f"msg_assign_{task_id}",
                sender="manager",
                recipient=f"worker:{worker_provider}",
                task_id=task_id,
                msg_type="assign",
                body=f"Execute task #{task_id}: {title}"
            ))

            # Start worker in progress tracker
            agent_key = f"{worker_provider}_{session_id}"
            self.progress.agent_started(
                agent_id=agent_key,
                provider=worker_provider,
                role=role,
                task_id=task_id,
                task_title=title,
                session_id=session_id
            )
            self._notify_progress()

            # Execute worker adapter
            adapter = self.adapters.get(worker_provider)
            if not adapter:
                self.budget.release(task_id)
                failed_count += 1
                break

            exec_res = adapter.run(
                prompt=prompt,
                cwd=self.project_dir,
                task_id=task_id,
                session_id=session_id
            )

            self.hub.append_log(task_id, f"WORKER EXECUTION ({worker_provider}):\n{exec_res.output}")
            if exec_res.error:
                self.hub.append_log(task_id, f"WORKER STDERR:\n{exec_res.error}")

            self.budget.release(task_id)
            self.progress.agent_finished(agent_key, task_id, exec_res.success)
            self._notify_progress()

            if not exec_res.success:
                failed_count += 1
                err_summary = exec_res.error or "Worker failed without error output"
                self.progress.set_task_status(task_id, "failed", err_summary)
                self.hub.update_task_status(task_id, title, worker_provider, "failed", err_summary, role=role)
                self._notify_progress()
                break

            worker_output = exec_res.output
            self.hub.append_task_result(task_id, f"Worker Output ({worker_provider})", worker_output)

            # Send result message to inbox
            self.hub.send_message(InboxMessage(
                msg_id=f"msg_res_{task_id}",
                sender=f"worker:{worker_provider}",
                recipient="manager",
                task_id=task_id,
                msg_type="result",
                body=worker_output[:500]  # snippet in inbox
            ))

            # Review phase: select reviewer (different provider or separate clean session)
            reviewer_info = self.router.select_reviewer(worker_provider)
            if reviewer_info and role != "reviewer":
                rev_provider, rev_session, is_distinct = reviewer_info
                self.progress.set_task_status(task_id, "reviewing")

                # Budget check for reviewer
                try:
                    rev_allowed, rev_warn = self.budget.check_and_acquire(f"rev_{task_id}", rev_provider)
                    if rev_warn:
                        self.blockers.append(rev_warn)
                except Exception as e:
                    self.blockers.append(str(e))
                    rev_allowed = False

                if rev_allowed:
                    rev_agent_key = f"rev_{rev_provider}_{rev_session}"
                    self.progress.agent_started(
                        agent_id=rev_agent_key,
                        provider=rev_provider,
                        role="reviewer",
                        task_id=task_id,
                        task_title=f"Review of {title}",
                        session_id=rev_session
                    )
                    self._notify_progress()

                    review_prompt = (
                        f"Review the following output for task '{title}'.\n"
                        f"Ensure requirements are met with zero hallucinations or missing details:\n\n"
                        f"{worker_output}\n\n"
                        f"Provide your verdict: APPROVED or REVISE, and short justification."
                    )

                    rev_adapter = self.adapters.get(rev_provider)
                    rev_res = rev_adapter.run(
                        prompt=review_prompt,
                        cwd=self.project_dir,
                        task_id=task_id,
                        session_id=rev_session
                    ) if rev_adapter else None

                    self.budget.release(f"rev_{task_id}")
                    self.progress.agent_finished(rev_agent_key, task_id, True)

                    if rev_res:
                        self.hub.append_log(task_id, f"REVIEWER OUTPUT ({rev_provider}):\n{rev_res.output}")
                        self.hub.append_task_result(task_id, f"Review ({rev_provider})", rev_res.output)
                        self.hub.send_message(InboxMessage(
                            msg_id=f"msg_rev_{task_id}",
                            sender=f"reviewer:{rev_provider}",
                            recipient="manager",
                            task_id=task_id,
                            msg_type="review",
                            body=rev_res.output[:300]
                        ))

            # Mark task finished & archive
            short_summary = worker_output.split("\n")[0][:80]
            self.hub.update_task_status(
                task_id=task_id,
                title=title,
                worker=worker_provider,
                state="done",
                one_line_summary=short_summary,
                role=role
            )
            self.hub.archive_task(task_id, f"{title} completed successfully: {short_summary}")
            self.progress.set_task_status(task_id, "done", short_summary)
            completed_count += 1
            previous_output = worker_output
            self._notify_progress()

        total_elapsed = time.time() - start_time
        overall_success = (failed_count == 0 and completed_count == len(tasks))

        # Final short summary report for the manager
        summary_lines = [
            f"Orchestration completed in {int(total_elapsed)}s.",
            f"Tasks: {completed_count}/{len(tasks)} succeeded.",
            f"Goal: {goal}"
        ]
        if self.blockers:
            summary_lines.append(f"Alerts/Blockers: {len(self.blockers)}")

        report = "\n".join(summary_lines)

        return OrchestratorRunResult(
            success=overall_success,
            goal=goal,
            elapsed_seconds=total_elapsed,
            tasks_completed=completed_count,
            tasks_failed=failed_count,
            summary_report=report,
            blockers=list(self.blockers)
        )
