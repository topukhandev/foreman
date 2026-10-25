"""Project registry and relationship model (~/.foreman/projects.yaml).

Deterministic and cheap: projects are found under configured roots; relationships come from three sources:
  declared   - added by the user (`foreman projects link`)
  discovered - static evidence in files (paths, module/package names, mentions)
  learned    - reported by workers during tasks, with evidence
Every relation records when it was verified and the git HEADs at that time, so the engine can tell
reliable knowledge from knowledge that must be re-checked.
"""

import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

import yaml

from orchestrator.util import atomic_write, git_head, now_iso, read_text, short

MANIFESTS = {
    "package.json": "node", "composer.json": "php", "go.mod": "go", "pyproject.toml": "python",
    "requirements.txt": "python", "setup.py": "python", "Cargo.toml": "rust", "pom.xml": "java",
    "build.gradle": "java", "Gemfile": "ruby",
}
SKIP_DIRS = {"node_modules", "vendor", ".git", "dist", "build", "__pycache__", ".venv", "venv", "storage", "public"}
EVIDENCE_FILES = ["go.mod", "composer.json", "package.json", ".env.example", ".env.sample", "hub.yaml",
                  "docker-compose.yml", "docker-compose.yaml", "compose.yaml"]
RELATION_TYPES = ["depends_on", "api_client_of", "tests", "shares_contract_with", "deploys", "related", "mentions"]


@dataclass
class Project:
    name: str
    path: str
    stack: List[str] = field(default_factory=list)
    summary: str = ""
    test: Optional[str] = None
    checks: Dict[str, str] = field(default_factory=dict)   # lint / typecheck / build / e2e commands (test is `test`)
    components: Dict[str, List[str]] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    head: Optional[str] = None
    scanned_at: str = ""
    source: str = "discovered"


@dataclass
class Relation:
    src: str
    dst: str
    type: str
    evidence: str = ""
    source: str = "discovered"       # declared | discovered | learned
    confidence: str = "medium"       # high | medium | low
    verified_at: str = ""
    heads: Dict[str, Optional[str]] = field(default_factory=dict)
    task: Optional[str] = None


@dataclass
class Selection:
    anchor: Optional[Project]
    focus: List[Project]
    neighbors: List[Tuple[Relation, Project]]
    others: List[Project]


class Registry:
    def __init__(self, home: str, roots: List[str]):
        self.path = os.path.join(home, "projects.yaml")
        self.roots = roots
        self.projects: Dict[str, Project] = {}
        self.relations: List[Relation] = []
        self._lock = threading.RLock()
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except OSError:
            data = {}
        self.projects = {n: Project(name=n, **{k: v for k, v in (p or {}).items() if k in Project.__dataclass_fields__ and k != "name"})
                         for n, p in (data.get("projects") or {}).items()}
        self.relations = [Relation(**{k: v for k, v in r.items() if k in Relation.__dataclass_fields__})
                          for r in (data.get("relations") or [])]

    def save(self) -> None:
        with self._lock:
            data = {
                "projects": {n: {k: v for k, v in asdict(p).items() if k != "name"} for n, p in sorted(self.projects.items())},
                "relations": [asdict(r) for r in self.relations],
            }
            atomic_write(self.path, "# Foreman project registry. Safe to edit; `source: declared` entries are never overwritten.\n"
                         + yaml.safe_dump(data, sort_keys=False, allow_unicode=True))

    # ------------------------------------------------------------------ discovery
    def ensure_scanned(self, cwd: Optional[str] = None) -> None:
        """Scan once; and recognise the directory the user works in, even if it was created after the last scan."""
        if not self.projects:
            self.scan()
        if cwd:
            self.adopt(cwd)

    def adopt(self, path: str) -> Optional[Project]:
        """The project that holds path. A folder directly under a project root that is not known yet (a new project,
        possibly still empty, no git or manifest) is registered as declared, so work started there stays there."""
        path = os.path.abspath(path)
        known = self.project_for_path(path)
        if known:
            return known
        for root in self.roots:
            root = os.path.abspath(os.path.expanduser(root)).rstrip("/")
            if not path.startswith(root + "/"):
                continue
            top = os.path.join(root, path[len(root) + 1:].split("/")[0])
            base = os.path.basename(top)
            if not os.path.isdir(top) or base.startswith(".") or base in SKIP_DIRS:
                return None
            with self._lock:
                name = base if base not in self.projects else f"{base}@{os.path.basename(root)}"
                proj = self._describe(name, top, None)
                proj.source = "declared"
                self.projects[name] = proj
                self._discover_relations()
                self.save()
                return proj
        return None

    def scan(self) -> List[str]:
        """Discover projects under roots and refresh discovered relations. Returns project names found."""
        with self._lock:
            found: Dict[str, str] = {}
            for root in self.roots:
                if not os.path.isdir(root):
                    continue
                for entry in sorted(os.listdir(root)):
                    full = os.path.join(root, entry)
                    if entry.startswith(".") or entry in SKIP_DIRS or not os.path.isdir(full):
                        continue
                    if _looks_like_project(full):
                        name = entry if entry not in found else f"{entry}@{os.path.basename(root)}"
                        found[name] = full
            for p in self.projects.values():
                if p.source == "declared" and os.path.isdir(p.path):
                    found.setdefault(p.name, p.path)
            known_paths = {p.path: n for n, p in self.projects.items()}
            for name, path in found.items():
                existing = self.projects.get(known_paths.get(path, name))
                self.projects[existing.name if existing else name] = self._describe(existing.name if existing else name, path, existing)
            self.projects = {n: p for n, p in self.projects.items() if os.path.isdir(p.path)}
            self._discover_relations()
            self.save()
            return sorted(self.projects)

    def add_project(self, path: str, name: Optional[str] = None) -> Project:
        path = os.path.abspath(os.path.expanduser(path))
        with self._lock:
            name = name or os.path.basename(path.rstrip("/"))
            proj = self._describe(name, path, self.projects.get(name))
            proj.source = "declared"
            self.projects[name] = proj
            self._discover_relations()
            self.save()
            return proj

    def refresh(self, names: List[str]) -> List[str]:
        """Re-describe projects whose HEAD moved since the last scan. Returns names that changed."""
        changed = []
        with self._lock:
            for n in names:
                p = self.projects.get(n)
                if p and os.path.isdir(p.path) and git_head(p.path) != p.head:
                    self.projects[n] = self._describe(n, p.path, p)
                    changed.append(n)
            if changed:
                self._discover_relations()
                self.save()
        return changed

    def _describe(self, name: str, path: str, prev: Optional[Project]) -> Project:
        stack, components = _stack(path), {}
        for sub in sorted(os.listdir(path)) if os.path.isdir(path) else []:
            sp = os.path.join(path, sub)
            if sub.startswith(".") or sub in SKIP_DIRS or not os.path.isdir(sp):
                continue
            s = _stack(sp)
            if s:
                components[sub] = s
        test = _detect_test(path)
        if not test:
            tests = [f"cd {c} && {t}" for c in components for t in [_detect_test(os.path.join(path, c))] if t]
            test = " ; ".join(tests) if tests else None
        return Project(
            name=name, path=path,
            stack=sorted(set(stack + [s for v in components.values() for s in v])),
            summary=_summary(path) or (prev.summary if prev else ""),
            test=(prev.test if prev and prev.source == "declared" and prev.test else test),
            checks=_detect_checks(path),
            components=components,
            aliases=prev.aliases if prev else [],
            notes=prev.notes if prev else [],
            head=git_head(path), scanned_at=now_iso(),
            source=prev.source if prev else "discovered",
        )

    def _discover_relations(self) -> None:
        kept = [r for r in self.relations if r.source != "discovered"]
        found: Dict[Tuple[str, str, str], Relation] = {}
        identities = {n: _identities(p) for n, p in self.projects.items()}
        # Module names shared by several projects (copies/forks of one repo) identify none of them.
        counts: Dict[str, int] = {}
        for ids in identities.values():
            for m in set(ids["modules"]):
                counts[m] = counts.get(m, 0) + 1
        for ids in identities.values():
            ids["modules"] = [m for m in ids["modules"] if counts[m] == 1]
        for src, p in self.projects.items():
            for rel_file, text in _evidence_texts(p.path):
                for dst, ids in identities.items():
                    if dst == src:
                        continue
                    hit = _match(text, ids, rel_file)
                    if hit:
                        rtype, conf, line = hit
                        key = (src, dst, rtype)
                        if key not in found or _rank(conf) > _rank(found[key].confidence):
                            found[key] = Relation(src, dst, rtype, f"{rel_file}: {short(line, 140)}", "discovered", conf,
                                                  now_iso(), {src: p.head, dst: self.projects[dst].head})
        # A strong relation makes a weaker mention between the same pair redundant.
        strong = {(r.src, r.dst) for r in found.values() if r.type != "mentions"}
        discovered = [r for r in found.values() if not (r.type == "mentions" and (r.src, r.dst) in strong)]
        manual = {(r.src, r.dst) for r in kept}
        self.relations = kept + [r for r in discovered if (r.src, r.dst) not in manual]

    # ------------------------------------------------------------------ relations
    def link(self, src: str, dst: str, rtype: str, note: str = "", source: str = "declared",
             confidence: str = "high", task: Optional[str] = None) -> Relation:
        with self._lock:
            for r in self.relations:
                if r.src == src and r.dst == dst and r.type == rtype:
                    if source == "declared" or r.source != "declared":
                        r.source, r.confidence = source, confidence
                    r.evidence = note or r.evidence
                    r.verified_at, r.task = now_iso(), task or r.task
                    r.heads = self._heads(src, dst)
                    self.save()
                    return r
            # A learned/declared relation supersedes discovered guesses between the same pair.
            self.relations = [r for r in self.relations
                              if not (r.src == src and r.dst == dst and r.source == "discovered")]
            rel = Relation(src, dst, rtype, note, source, confidence, now_iso(), self._heads(src, dst), task)
            self.relations.append(rel)
            self.save()
            return rel

    def unlink(self, src: str, dst: str) -> int:
        with self._lock:
            before = len(self.relations)
            self.relations = [r for r in self.relations if not ({r.src, r.dst} == {src, dst})]
            self.save()
            return before - len(self.relations)

    def mark_verified(self, src: str, dst: str) -> None:
        with self._lock:
            for r in self.relations:
                if {r.src, r.dst} == {src, dst}:
                    r.verified_at, r.heads = now_iso(), self._heads(r.src, r.dst)
            self.save()

    def relation_is_fresh(self, r: Relation) -> bool:
        return all(git_head(self.projects[n].path) == h for n, h in r.heads.items() if n in self.projects)

    def _heads(self, *names: str) -> Dict[str, Optional[str]]:
        return {n: git_head(self.projects[n].path) for n in names if n in self.projects}

    def relations_of(self, name: str) -> List[Relation]:
        return [r for r in self.relations if name in (r.src, r.dst)]

    # ------------------------------------------------------------------ lookup / selection
    def project_for_path(self, path: str) -> Optional[Project]:
        path = os.path.abspath(path)
        best = None
        for p in self.projects.values():
            if path == p.path or path.startswith(p.path.rstrip("/") + "/"):
                if not best or len(p.path) > len(best.path):
                    best = p
        return best

    def find(self, ref: str) -> Optional[Project]:
        if ref in self.projects:
            return self.projects[ref]
        if os.path.isdir(os.path.expanduser(ref)):
            return self.project_for_path(os.path.expanduser(ref))
        low = ref.lower()
        for p in self.projects.values():
            if low == p.name.lower() or low in [a.lower() for a in p.aliases]:
                return p
        return None

    def mentioned_in(self, text: str) -> List[Project]:
        low = text.lower()
        norm = re.sub(r"[^a-z0-9]+", " ", low)
        scored = []
        for p in self.projects.values():
            score = 0
            if p.path in text:
                score = 10
            for ident in [p.name] + p.aliases:
                il = ident.lower()
                if re.search(r"(?<![a-z0-9])" + re.escape(il) + r"(?![a-z0-9])", low):
                    score = max(score, 10)
                spaced = re.sub(r"[^a-z0-9]+", " ", il).strip()
                if spaced and len(spaced) >= 4 and f" {spaced} " in f" {norm} ":
                    score = max(score, 8)
            if score:
                scored.append((score, p))
        return [p for _, p in sorted(scored, key=lambda x: -x[0])]

    def select(self, request: str, cwd: str, extra: Optional[List[str]] = None) -> Selection:
        anchor = self.project_for_path(cwd)
        focus: List[Project] = []
        for p in ([anchor] if anchor else []) + self.mentioned_in(request) + [self.projects[n] for n in (extra or []) if n in self.projects]:
            if p and p.name not in [f.name for f in focus]:
                focus.append(p)
        names = {p.name for p in focus}
        neighbors: List[Tuple[Relation, Project]] = []
        for r in self.relations:
            for a, b in ((r.src, r.dst), (r.dst, r.src)):
                if a in names and b not in names and b in self.projects:
                    neighbors.append((r, self.projects[b]))
        near = names | {p.name for _, p in neighbors}
        others = [p for n, p in sorted(self.projects.items()) if n not in near]
        return Selection(anchor, focus, neighbors, others)

    def render_context(self, sel: Selection) -> str:
        """Compact project cards for the planner. One line per project; relations with freshness."""
        lines = []
        if sel.focus:
            lines.append("Projects in focus (current directory or named in the request):")
            for p in sel.focus:
                lines.append(self._card(p, anchor=(sel.anchor is not None and p.name == sel.anchor.name)))
                for r in self.relations_of(p.name):
                    lines.append("    " + self._rel_line(r, p.name))
        if sel.neighbors:
            lines.append("Related projects (known relations; relevance to this request not yet established):")
            seen = set()
            for _, p in sel.neighbors:
                if p.name not in seen:
                    seen.add(p.name)
                    lines.append(self._card(p))
        if sel.others:
            if sel.focus:
                lines.append("Other known projects (no known relation): " + ", ".join(p.name for p in sel.others))
            else:
                lines.append("Known projects (none matched the request or current directory):")
                lines += [self._card(p) for p in sel.others]
        return "\n".join(lines) or "No projects registered."

    def _card(self, p: Project, anchor: bool = False) -> str:
        comp = (" components: " + ", ".join(f"{c}({'/'.join(s)})" for c, s in p.components.items())) if p.components else ""
        test = f" | test: `{p.test}`" if p.test else ""
        test += "".join(f" | {k}: `{v}`" for k, v in p.checks.items())
        notes = (" | notes: " + "; ".join(p.notes[-3:])) if p.notes else ""
        tag = " [current directory]" if anchor else ""
        return f"- {p.name}{tag}: {p.path} [{', '.join(p.stack) or '?'}]{comp}{test} — {short(p.summary, 140)}{notes}"

    def _rel_line(self, r: Relation, viewpoint: str) -> str:
        arrow = f"{r.src} --{r.type}--> {r.dst}"
        fresh = "verified, unchanged since" if self.relation_is_fresh(r) else "code changed since verification - re-verify if it matters"
        return f"{arrow} ({r.source}, {r.confidence}; {fresh}; evidence: {short(r.evidence, 120)})"


# ---------------------------------------------------------------------- helpers

def _looks_like_project(path: str) -> bool:
    if os.path.isdir(os.path.join(path, ".git")) or _stack(path):
        return True
    try:
        return any(_stack(os.path.join(path, s)) for s in os.listdir(path)
                   if not s.startswith(".") and s not in SKIP_DIRS and os.path.isdir(os.path.join(path, s)))
    except OSError:
        return False


def _stack(path: str) -> List[str]:
    return sorted({lang for f, lang in MANIFESTS.items() if os.path.isfile(os.path.join(path, f))})


def _detect_test(path: str) -> Optional[str]:
    pkg = os.path.join(path, "package.json")
    if os.path.isfile(pkg):
        try:
            script = (json.loads(read_text(pkg)).get("scripts") or {}).get("test", "")
        except ValueError:
            script = ""
        if script and "no test specified" not in script:
            runner = "pnpm" if os.path.exists(os.path.join(path, "pnpm-lock.yaml")) else \
                     "yarn" if os.path.exists(os.path.join(path, "yarn.lock")) else "npm"
            return f"{runner} test"
    if os.path.isfile(os.path.join(path, "artisan")):
        return "php artisan test"
    if os.path.isfile(os.path.join(path, "phpunit.xml")) or os.path.isfile(os.path.join(path, "phpunit.xml.dist")):
        return "vendor/bin/phpunit"
    if os.path.isfile(os.path.join(path, "go.mod")):
        return "go test ./..."
    if os.path.isfile(os.path.join(path, "Cargo.toml")):
        return "cargo test"
    mk = read_text(os.path.join(path, "Makefile"), 50_000)
    if re.search(r"^test:", mk, re.M):
        return "make test"
    if any(os.path.exists(os.path.join(path, f)) for f in ("pytest.ini", "pyproject.toml", "setup.py")) and \
            os.path.isdir(os.path.join(path, "tests")):
        return "python3 -m pytest -q"
    return None


def _detect_checks(path: str) -> Dict[str, str]:
    """Lint / type-check / build / end-to-end commands a project already defines (never invented)."""
    out: Dict[str, str] = {}
    pkg = os.path.join(path, "package.json")
    if os.path.isfile(pkg):
        try:
            scripts = json.loads(read_text(pkg)).get("scripts") or {}
        except ValueError:
            scripts = {}
        runner = "pnpm" if os.path.exists(os.path.join(path, "pnpm-lock.yaml")) else \
                 "yarn" if os.path.exists(os.path.join(path, "yarn.lock")) else "npm"
        for kind, names in (("lint", ["lint"]), ("typecheck", ["typecheck", "type-check", "tsc", "check-types"]),
                            ("build", ["build"]), ("e2e", ["test:e2e", "e2e", "test-e2e"])):
            name = next((n for n in names if n in scripts), None)
            if name:
                out[kind] = f"{runner} run {name}"
    if os.path.isfile(os.path.join(path, "vendor/bin/pint")):
        out.setdefault("lint", "vendor/bin/pint --test")
    if any(os.path.isfile(os.path.join(path, f)) for f in ("phpstan.neon", "phpstan.neon.dist")):
        out.setdefault("typecheck", "vendor/bin/phpstan analyse --no-progress")
    pyproject = read_text(os.path.join(path, "pyproject.toml"), 50_000)
    if "[tool.ruff" in pyproject:
        out.setdefault("lint", "ruff check .")
    if "[tool.mypy" in pyproject:
        out.setdefault("typecheck", "mypy .")
    if os.path.isfile(os.path.join(path, "go.mod")):
        out.setdefault("lint", "go vet ./...")
        out.setdefault("build", "go build ./...")
    if os.path.isfile(os.path.join(path, "Cargo.toml")):
        out.setdefault("lint", "cargo clippy --quiet")
        out.setdefault("build", "cargo build --quiet")
    return out


def _summary(path: str) -> str:
    for name in ("README.md", "readme.md", "README", "README.rst"):
        text = read_text(os.path.join(path, name), 20_000)
        if not text:
            continue
        for para in re.split(r"\n\s*\n", text):
            p = para.strip()
            if not p or p.startswith(("#", "![", "[!", "<", "```", "|", "---", "===")):
                continue
            p = re.sub(r"\*\*|__|`|^>\s*", "", p)
            return short(p, 200)
    return ""


def _identities(p: Project) -> Dict[str, List[str]]:
    """Strings that, found in another project's files, indicate a relation to p."""
    paths = [p.path]
    home = os.path.expanduser("~")
    if p.path.startswith(home + "/"):
        paths.append("~" + p.path[len(home):])
    modules = []
    gomod = read_text(os.path.join(p.path, "go.mod"), 5000)
    m = re.search(r"^module\s+(\S+)", gomod, re.M)
    if m:
        modules.append(m.group(1))
    for manifest in [os.path.join(p.path, "package.json")] + [os.path.join(p.path, c, "package.json") for c in p.components]:
        try:
            name = json.loads(read_text(manifest)).get("name", "") if os.path.isfile(manifest) else ""
        except ValueError:
            name = ""
        if name and ("/" in name or "-" in name) and len(name) >= 6:
            modules.append(name)
    for composer in [os.path.join(p.path, "composer.json")] + [os.path.join(p.path, c, "composer.json") for c in p.components]:
        try:
            name = json.loads(read_text(composer)).get("name", "") if os.path.isfile(composer) else ""
        except ValueError:
            name = ""
        if name and "/" in name and not name.startswith("laravel/"):
            modules.append(name)
    # Bare-name mentions only count for distinctive compound names ("orders-api", not "orchestrator").
    names = ([p.name] if re.search(r"[-_.]", p.name) and len(p.name) >= 6 else []) + p.aliases
    return {"paths": paths, "modules": modules, "names": names}


def _evidence_texts(path: str):
    files = []
    for base in [path] + [os.path.join(path, s) for s in sorted(os.listdir(path))
                          if os.path.isdir(os.path.join(path, s)) and not s.startswith(".") and s not in SKIP_DIRS]:
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        mds = [e for e in entries if e.lower().endswith(".md")][:25]
        for e in EVIDENCE_FILES + mds:
            fp = os.path.join(base, e)
            if os.path.isfile(fp):
                files.append(fp)
        if base == path and os.path.isdir(os.path.join(path, "docs")):
            files += [os.path.join(path, "docs", e) for e in sorted(os.listdir(os.path.join(path, "docs")))[:15]
                      if e.lower().endswith((".md", ".yaml", ".yml"))]
    for fp in files[:80]:
        yield os.path.relpath(fp, path), read_text(fp, 100_000)


def _match(text: str, ids: Dict[str, List[str]], rel_file: str):
    for p in ids["paths"]:
        idx = text.find(p)
        if idx != -1 and not text[idx + len(p): idx + len(p) + 1].isalnum() and text[idx + len(p): idx + len(p) + 1] not in "-_":
            return "related", "high", _line_at(text, idx)
    for mod in ids["modules"]:
        m = re.search(r"(?<![A-Za-z0-9_.@/-])" + re.escape(mod) + r"(?![A-Za-z0-9_-])", text)
        if m:
            idx = m.start()
            dependent = os.path.basename(rel_file) in ("go.mod", "package.json", "composer.json")
            return ("depends_on" if dependent else "related"), "high", _line_at(text, idx)
    for name in ids["names"]:
        m = re.search(r"(?<![A-Za-z0-9_/-])" + re.escape(name) + r"(?![A-Za-z0-9_-])", text)
        if m:
            return "mentions", "low", _line_at(text, m.start())
    return None


def _line_at(text: str, idx: int) -> str:
    start = text.rfind("\n", 0, idx) + 1
    end = text.find("\n", idx)
    return text[start: end if end != -1 else len(text)].strip()


def _rank(conf: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get(conf, 0)
