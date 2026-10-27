"""Dependency manifests (deterministic): which dependencies a project declares, so Foreman can see the ones a task
added. New dependencies are never added silently: the user approves each one (engine._dependency_gate)."""

import json
import os
import re
from typing import Dict, Set

MANIFESTS = ("package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "composer.json", "go.mod",
             "Cargo.toml", "Gemfile")
_NAME = re.compile(r"[A-Za-z0-9@][A-Za-z0-9@/._-]*")


def read_manifests(path: str) -> Dict[str, str]:
    out = {}
    for name in MANIFESTS:
        try:
            with open(os.path.join(path, name), encoding="utf-8", errors="replace") as f:
                out[name] = f.read(200_000)
        except OSError:
            continue
    return out


def declared(name: str, text: str) -> Set[str]:
    """Dependency names a manifest declares (best effort; an unreadable file declares nothing)."""
    try:
        if name in ("package.json", "composer.json"):
            data = json.loads(text)
            keys = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies") \
                if name == "package.json" else ("require", "require-dev")
            return {k for key in keys for k in (data.get(key) or {}) if isinstance(data.get(key), dict)}
    except ValueError:
        return set()
    if name.startswith("requirements"):
        return {m.group(0).lower() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "-")) and (m := _NAME.match(line.strip()))}
    if name == "pyproject.toml":
        found = set()
        for block in re.findall(r"(?ms)^\s*(?:optional-)?dependencies\s*=\s*\[(.*?)\]", text):
            found |= {m.lower() for m in re.findall(r"[\"']\s*([A-Za-z0-9][A-Za-z0-9._-]*)", block)}
        for section in re.findall(r"(?ms)^\[tool\.poetry\.(?:dev-)?dependencies\]\s*$(.*?)(?=^\[|\Z)", text):
            found |= {m.lower() for m in re.findall(r"(?m)^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", section)} - {"python"}
        return found
    if name == "go.mod":
        found = set(re.findall(r"(?m)^\s*require\s+(\S+)\s+v", text))
        for block in re.findall(r"(?ms)^require\s*\((.*?)\)", text):
            found |= set(re.findall(r"(?m)^\s*(\S+)\s+v", block))
        return found
    if name == "Cargo.toml":
        found = set()
        for section in re.findall(r"(?ms)^\[(?:dev-|build-)?dependencies\]\s*$(.*?)(?=^\[|\Z)", text):
            found |= set(re.findall(r"(?m)^\s*([A-Za-z0-9_-]+)\s*=", section))
        return found
    if name == "Gemfile":
        return set(re.findall(r"(?m)^\s*gem\s+['\"]([^'\"]+)", text))
    return set()


def added(before: Dict[str, str], path: str) -> Dict[str, Set[str]]:
    """Dependencies declared now that were not declared when the task started: {manifest: names}."""
    out = {}
    for name, text in read_manifests(path).items():
        new = declared(name, text) - declared(name, before.get(name, ""))
        if new:
            out[name] = new
    return out
