"""System Resource Guard: Monitors RAM, CPU, Load Average, and Storage
to prevent OS freezing, memory exhaustion, or panic crashes during multi-worker execution.
"""

from dataclasses import dataclass
import os
import platform
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SystemMetrics:
    ram_total_mb: float
    ram_available_mb: float
    ram_used_percent: float
    cpu_count: int
    cpu_percent: float
    load_avg_1m: float
    disk_free_gb: float
    disk_total_gb: float
    disk_used_percent: float
    is_healthy: bool
    status: str          # "HEALTHY" | "WARNING" | "CRITICAL"
    status_message: str
    safe_max_workers: int


class SystemGuard:
    """
    Deterministic hardware & OS resource checker.
    Prevents machine panic / lockup by evaluating real-time RAM, CPU, and disk headroom.
    """

    MIN_FREE_RAM_MB: float = 400.0      # Critical safety floor: never spawn if RAM < 400 MB
    RAM_PER_WORKER_MB: float = 150.0    # Approximate RAM consumption per AI CLI process
    MAX_CPU_PERCENT: float = 95.0       # Throttle threshold when CPU is pegged
    MIN_DISK_FREE_GB: float = 0.5       # At least 500 MB free disk space

    @classmethod
    def get_metrics(cls, target_dir: str = ".") -> SystemMetrics:
        """
        Measure real-time system metrics using psutil (if available) or OS fallbacks.
        Cross-platform (Linux, macOS, Windows).
        """
        # 1. RAM detection
        ram_total_mb = 4096.0
        ram_available_mb = 2048.0
        ram_used_pct = 50.0
        cpu_pct = 15.0
        cpu_cnt = os.cpu_count() or 4
        load_1m = 1.0

        try:
            import psutil
            vm = psutil.virtual_memory()
            ram_total_mb = vm.total / (1024 * 1024)
            ram_available_mb = vm.available / (1024 * 1024)
            ram_used_pct = vm.percent
            cpu_pct = psutil.cpu_percent(interval=0.05)
            cpu_cnt = psutil.cpu_count(logical=True) or cpu_cnt
        except Exception:
            # Linux fallback via /proc/meminfo
            if os.path.exists("/proc/meminfo"):
                try:
                    mem_data = {}
                    with open("/proc/meminfo", "r") as f:
                        for line in f:
                            parts = line.split(":")
                            if len(parts) == 2:
                                k = parts[0].strip()
                                v = parts[1].strip().split()[0]
                                mem_data[k] = float(v)  # in kB
                    total_kb = mem_data.get("MemTotal", 4096 * 1024)
                    avail_kb = mem_data.get("MemAvailable", mem_data.get("MemFree", 2048 * 1024))
                    ram_total_mb = total_kb / 1024.0
                    ram_available_mb = avail_kb / 1024.0
                    ram_used_pct = ((total_kb - avail_kb) / total_kb) * 100.0
                except Exception:
                    pass

        # 2. CPU load average
        if hasattr(os, "getloadavg"):
            try:
                loads = os.getloadavg()
                load_1m = loads[0]
                # If psutil wasn't available, approximate cpu_pct from 1m load
                if "psutil" not in sys.modules:
                    cpu_pct = min(100.0, (load_1m / max(1, cpu_cnt)) * 100.0)
            except Exception:
                load_1m = 1.0

        # 3. Disk space
        check_path = target_dir if os.path.exists(target_dir) else "."
        try:
            usage = shutil.disk_usage(check_path)
            disk_total_gb = usage.total / (1024 ** 3)
            disk_free_gb = usage.free / (1024 ** 3)
            disk_used_pct = ((usage.total - usage.free) / usage.total) * 100.0
        except Exception:
            disk_total_gb = 100.0
            disk_free_gb = 50.0
            disk_used_pct = 50.0

        # 4. Compute safe capacity and status
        # Free RAM headroom after reserving MIN_FREE_RAM_MB
        ram_headroom = max(0.0, ram_available_mb - cls.MIN_FREE_RAM_MB)
        safe_workers = min(16, int(ram_headroom // cls.RAM_PER_WORKER_MB))

        status = "HEALTHY"
        messages = []

        if ram_available_mb < cls.MIN_FREE_RAM_MB:
            status = "CRITICAL"
            messages.append(f"RAM critically low ({ram_available_mb:.0f} MB available). Risk of OS freeze.")
            safe_workers = 0
        elif ram_available_mb < 800.0:
            status = "WARNING"
            messages.append(f"RAM headroom is tight ({ram_available_mb:.0f} MB available).")

        if cpu_pct >= cls.MAX_CPU_PERCENT:
            status = "CRITICAL"
            messages.append(f"CPU is pegged at {cpu_pct:.0f}% load.")
            safe_workers = min(1, safe_workers)
        elif cpu_pct >= 85.0:
            if status != "CRITICAL":
                status = "WARNING"
            messages.append(f"High CPU utilization ({cpu_pct:.0f}%).")

        if disk_free_gb < cls.MIN_DISK_FREE_GB:
            status = "CRITICAL"
            messages.append(f"Disk space nearly exhausted ({disk_free_gb * 1024:.0f} MB free).")
            safe_workers = 0

        if not messages:
            status_msg = f"System resources optimal (Up to {safe_workers} parallel workers safe)."
        else:
            status_msg = " | ".join(messages)

        return SystemMetrics(
            ram_total_mb=ram_total_mb,
            ram_available_mb=ram_available_mb,
            ram_used_percent=ram_used_pct,
            cpu_count=cpu_cnt,
            cpu_percent=cpu_pct,
            load_avg_1m=load_1m,
            disk_free_gb=disk_free_gb,
            disk_total_gb=disk_total_gb,
            disk_used_percent=disk_used_pct,
            is_healthy=(status == "HEALTHY"),
            status=status,
            status_message=status_msg,
            safe_max_workers=safe_workers
        )

    @classmethod
    def can_spawn(cls, count: int = 1, target_dir: str = ".") -> Tuple[bool, Optional[str]]:
        """
        Pre-flight check to verify if spawning `count` AI workers is safe for this PC.
        Returns: (allowed: bool, reason_or_warning: Optional[str])
        """
        metrics = cls.get_metrics(target_dir=target_dir)

        if metrics.status == "CRITICAL":
            return False, (
                f"🛑 [System Guard Alert] Spawning halted to prevent system crash / freeze:\n"
                f"   • {metrics.status_message}\n"
                f"   • Available RAM : {metrics.ram_available_mb:.0f} MB / {metrics.ram_total_mb:.0f} MB\n"
                f"   • CPU Load      : {metrics.cpu_percent:.0f}%\n"
                f"   • Disk Free     : {metrics.disk_free_gb:.1f} GB"
            )

        # Check required RAM for requested worker count
        required_ram = cls.MIN_FREE_RAM_MB + (count * cls.RAM_PER_WORKER_MB)
        if metrics.ram_available_mb < required_ram:
            return False, (
                f"⚠️ [System Guard Alert] Insufficient RAM to safely spawn {count} workers:\n"
                f"   • Available RAM : {metrics.ram_available_mb:.0f} MB\n"
                f"   • Estimated RAM : ~{count * cls.RAM_PER_WORKER_MB:.0f} MB needed (+{cls.MIN_FREE_RAM_MB:.0f} MB safety buffer)\n"
                f"   • Safe Capacity : Spawning max {metrics.safe_max_workers} worker(s) recommended."
            )

        # Check disk space
        if metrics.disk_free_gb < cls.MIN_DISK_FREE_GB:
            return False, (
                f"🛑 [System Guard Alert] Low disk space on {target_dir}:\n"
                f"   • Free Space    : {metrics.disk_free_gb * 1024:.0f} MB (Minimum {cls.MIN_DISK_FREE_GB * 1024:.0f} MB required)."
            )

        warning = None
        if metrics.status == "WARNING":
            warning = f"⚠️ [System Guard Notice] System is under moderate load ({metrics.status_message}). Proceeding carefully."

        return True, warning

    @classmethod
    def render_report(cls, target_dir: str = ".") -> str:
        """Render a clean, formatted report of current system health and worker capacity."""
        m = cls.get_metrics(target_dir=target_dir)

        if m.status == "HEALTHY":
            badge = "\033[1;32m● HEALTHY\033[0m"
        elif m.status == "WARNING":
            badge = "\033[1;33m▲ WARNING\033[0m"
        else:
            badge = "\033[1;31m✖ CRITICAL (PANIC GUARD ACTIVE)\033[0m"

        lines = [
            "╔════════════════════════════════════════════════════════════════════════╗",
            f"║  🛡️  SYSTEM RESOURCE & SAFETY GUARD ({badge})",
            "╠════════════════════════════════════════════════════════════════════════╣",
            f"║  • RAM Available : {m.ram_available_mb:,.0f} MB free / {m.ram_total_mb:,.0f} MB ({m.ram_used_percent:.1f}% used)",
            f"║  • CPU Status    : {m.cpu_count} Cores | {m.cpu_percent:.1f}% Load (1m LoadAvg: {m.load_avg_1m:.2f})",
            f"║  • Disk Storage  : {m.disk_free_gb:.1f} GB free of {m.disk_total_gb:.1f} GB ({m.disk_used_percent:.1f}% used)",
            f"║  • Safe Capacity : Up to {m.safe_max_workers} concurrent AI workers safe to spawn",
            f"║  • Guard Status  : {m.status_message}",
            "╚════════════════════════════════════════════════════════════════════════╝"
        ]
        return "\n".join(lines)
