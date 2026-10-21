"""Configuration loader, validator, and setup detection for Orchestrator."""

from dataclasses import dataclass, field
import os
import platform
import shutil
import socket
from typing import Any, Dict, List, Optional
import yaml


@dataclass
class ProviderConfig:
    name: str
    enabled: bool = True
    tier: str = "paid"
    command: str = ""
    roles: List[str] = field(default_factory=list)
    limits: Dict[str, Any] = field(default_factory=dict)
    verified: bool = False
    verification_error: Optional[str] = None


@dataclass
class BudgetConfig:
    max_parallel: int = 2
    max_time_per_task_sec: int = 300
    max_retries: int = 3
    warn_at_percent: int = 80
    loop_detect_threshold: int = 3
    max_files_per_project: int = 1000
    max_project_size_mb: int = 500


@dataclass
class RoutingConfig:
    reviewer_must_differ_from_builder: bool = True
    fallback: str = "any_enabled"


@dataclass
class OrchestratorConfig:
    machine: str = ""
    os: str = "auto"
    providers: Dict[str, ProviderConfig] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    default_provider: str = ""
    visual_terminal: bool = True


DEFAULT_PROVIDERS_TEMPLATE = """# Orchestrator Local Providers Configuration
# Machine-specific settings - do not sync across different machines.
machine: "{hostname}"
os: auto

providers:
  antigravity_cli:
    enabled: true
    tier: paid
    command: "agy -p"
    roles: [planner, critic, coder, researcher, reviewer]
    limits:
      max_runs_per_day: 50
      max_parallel: 2

  claude_code:
    enabled: false
    tier: paid
    command: "claude -p"
    roles: [planner, critic, coder, reviewer]
    limits:
      max_runs_per_day: 50
      max_parallel: 2

  opencode:
    enabled: false
    tier: free
    command: "opencode run"
    roles: [bulk, researcher]
    limits:
      max_runs_per_day: 100
      max_parallel: 4

  mock:
    enabled: false
    tier: free
    command: "mock"
    roles: [planner, critic, coder, researcher, reviewer]
    limits:
      max_runs_per_day: 1000
      max_parallel: 4

routing:
  reviewer_must_differ_from_builder: true
  fallback: any_enabled

budget:
  max_parallel: 2
  max_time_per_task_sec: 300
  max_retries: 3
  warn_at_percent: 80
  loop_detect_threshold: 3
  max_files_per_project: 1000
  max_project_size_mb: 500
"""


def detect_os() -> str:
    """Return normalized OS string: linux, darwin, or windows."""
    sys_name = platform.system().lower()
    if "darwin" in sys_name or "mac" in sys_name:
        return "darwin"
    if "windows" in sys_name:
        return "windows"
    return "linux"


from orchestrator.finder import find_tool_executable


def resolve_command_binary(command_str: str) -> Optional[str]:
    """Extract binary name from command string and check if it exists in PATH, NVM, or user dirs."""
    parts = command_str.strip().split()
    if not parts:
        return None
    bin_name = parts[0]
    if bin_name.lower() == "mock":
        return "mock"
    return find_tool_executable(bin_name)


def init_default_config_file(target_path: str) -> None:
    """Create default providers.yaml if not present."""
    if os.path.exists(target_path):
        return
    hostname = socket.gethostname() or "local-pc"
    content = DEFAULT_PROVIDERS_TEMPLATE.format(hostname=hostname)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(content)


def load_config(config_path: str) -> OrchestratorConfig:
    """Load and validate providers.yaml, resolving binary paths and OS."""
    if not os.path.exists(config_path):
        init_default_config_file(config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    current_os = detect_os() if data.get("os", "auto") == "auto" else data.get("os", "auto")
    machine = data.get("machine") or socket.gethostname() or "local-pc"

    # Routing
    r_data = data.get("routing", {})
    routing = RoutingConfig(
        reviewer_must_differ_from_builder=r_data.get("reviewer_must_differ_from_builder", True),
        fallback=r_data.get("fallback", "any_enabled")
    )

    # Budget
    b_data = data.get("budget", {})
    budget = BudgetConfig(
        max_parallel=b_data.get("max_parallel", 2),
        max_time_per_task_sec=b_data.get("max_time_per_task_sec", 300),
        max_retries=b_data.get("max_retries", 3),
        warn_at_percent=b_data.get("warn_at_percent", 80),
        loop_detect_threshold=b_data.get("loop_detect_threshold", 3),
        max_files_per_project=b_data.get("max_files_per_project", 1000),
        max_project_size_mb=b_data.get("max_project_size_mb", 500)
    )

    # Providers
    providers: Dict[str, ProviderConfig] = {}
    p_data = data.get("providers", {})

    for p_name, p_info in p_data.items():
        enabled = bool(p_info.get("enabled", False))
        cmd = p_info.get("command", "").strip()
        roles = p_info.get("roles", [])
        tier = p_info.get("tier", "free")
        limits = p_info.get("limits", {})

        verified = False
        verr = None

        if enabled:
            if not cmd:
                enabled = False
                verr = "No command specified"
            else:
                bin_path = resolve_command_binary(cmd)
                if bin_path:
                    verified = True
                else:
                    enabled = False
                    parts = cmd.split()
                    bin_name = parts[0] if parts else cmd
                    verr = f"Command '{bin_name}' not found in PATH"

        providers[p_name] = ProviderConfig(
            name=p_name,
            enabled=enabled,
            tier=tier,
            command=cmd,
            roles=roles,
            limits=limits,
            verified=verified,
            verification_error=verr
        )

    default_p = str(data.get("default_provider", "")).strip()
    runtime_data = data.get("runtime", {})
    vis_term = bool(runtime_data.get("visual_terminal", data.get("visual_terminal", True)))

    return OrchestratorConfig(
        machine=machine,
        os=current_os,
        providers=providers,
        routing=routing,
        budget=budget,
        default_provider=default_p,
        visual_terminal=vis_term
    )
