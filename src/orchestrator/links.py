"""Multi-service and external folder linking for cross-project orchestration."""

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from typing import Dict, List, Optional


@dataclass
class LinkedService:
    name: str
    path: str
    description: str = ""
    added_at: str = ""

    def __post_init__(self):
        if not self.added_at:
            self.added_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class WorkspaceLinks:
    """Tracks connections to external microservices, repositories, and folders."""

    def __init__(self, project_dir: str):
        self.project_dir = os.path.abspath(project_dir)
        self.links_file = os.path.join(self.project_dir, "hub", "links.json")
        self.services: Dict[str, LinkedService] = {}
        self.load()

    def load(self) -> None:
        """Load linked services from hub/links.json."""
        if not os.path.exists(self.links_file):
            return
        try:
            with open(self.links_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for name, s_info in data.get("services", {}).items():
                    self.services[name] = LinkedService(**s_info)
        except Exception:
            self.services.clear()

    def save(self) -> None:
        """Save linked services to hub/links.json atomically."""
        os.makedirs(os.path.dirname(self.links_file), exist_ok=True)
        data = {
            "services": {name: asdict(svc) for name, svc in self.services.items()}
        }
        temp_path = self.links_file + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_path, self.links_file)

    def add_link(self, name: str, target_path: str, description: str = "") -> bool:
        """Link an external directory or service."""
        expanded_path = os.path.abspath(os.path.expanduser(target_path))
        if not os.path.exists(expanded_path):
            return False

        clean_name = name.strip() or os.path.basename(expanded_path)
        self.services[clean_name] = LinkedService(
            name=clean_name,
            path=expanded_path,
            description=description
        )
        self.save()
        return True

    def remove_link(self, name: str) -> bool:
        """Remove a linked service."""
        if name in self.services:
            del self.services[name]
            self.save()
            return True
        return False

    def list_links(self) -> List[LinkedService]:
        """Return list of linked services."""
        return list(self.services.values())
