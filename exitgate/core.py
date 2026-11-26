"""Can this AI coding session be closed without losing anything?

Two kinds of leak, deliberately separated:

  SESSION leaks block closing (exit 1). They exist only because this session is open and would be lost with
  it: uncommitted changes, a scratch script written and never run, files edited with no handoff note written
  since, an open item in a log the session was supposed to close out.

  SYSTEM leaks are reported, never blocking. A failing scheduled job or a full disk keeps leaking whether a
  session is open or not; holding the session open does not fix it.

A check that crashes is reported as a leak of its own kind, never silently passed: a crash that reads as
"safe" is the worst failure this tool can have.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .scratch import Bin

SESSION, SYSTEM = "session", "system"
CONFIG_NAMES = ("exitgate.toml", "exitgate.json")
DEFAULT_IGNORE = [".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache",
                  ".tox", "dist", "build", "state", "logs"]
DEFAULT_CHECKS = [
    {"type": "git_dirty", "repos": ["."]},
    {"type": "scratch_unrun"},
    {"type": "handoff", "watch": ["."]},
]


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------------------------- config

@dataclass
class Config:
    base: Path
    handoff: Path
    state_dir: Path
    scratch_dir: Path
    checks: list[dict]
    stale_after_hours: float = 6.0
    source: Path | None = None

    @classmethod
    def from_dict(cls, raw: dict, base: Path, source: Path | None = None) -> "Config":
        base = Path(base)
        rel = lambda v: (base / v) if not Path(v).is_absolute() else Path(v)  # noqa: E731
        checks = raw.get("check", raw.get("checks", DEFAULT_CHECKS))
        if not isinstance(checks, list):
            raise ConfigError("checks must be a list of tables")
        for i, c in enumerate(checks):
            if not isinstance(c, dict) or "type" not in c:
                raise ConfigError(f"check #{i + 1} needs a type")
        return cls(base=base, handoff=rel(raw.get("handoff", "HANDOFF.md")), state_dir=rel(raw.get("state_dir", "state")),
                   scratch_dir=rel(raw.get("scratch_dir", "scratch")), checks=checks,
                   stale_after_hours=float(raw.get("stale_after_hours", 6)), source=source)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        """--config path, else $EXITGATE_CONFIG, else ./exitgate.toml or ./exitgate.json, else built-in defaults."""
        p = Path(path) if path else (Path(os.environ["EXITGATE_CONFIG"]) if os.environ.get("EXITGATE_CONFIG") else None)
        if p is None:
            p = next((Path(n) for n in CONFIG_NAMES if Path(n).exists()), None)
        if p is None:
            return cls.from_dict({}, Path.cwd())
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {p}: {exc}") from exc
        if p.suffix == ".toml":
            try:
                import tomllib
            except ModuleNotFoundError as exc:            # Python 3.10
                raise ConfigError("TOML config needs Python 3.11+; use exitgate.json on 3.10") from exc
            try:
                raw = tomllib.loads(text)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{p}: {exc}") from exc
        else:
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{p}: {exc}") from exc
        return cls.from_dict(raw, p.resolve().parent, p)

    @property
    def cache(self) -> Path:
        return self.state_dir / "exit_gate.json"

    @property
    def marker(self) -> Path:
        return self.state_dir / "session.json"


# ---------------------------------------------------------------------------------------------- session start

def _parse_when(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def session_start(cfg: Config) -> tuple[float, str]:
    """(epoch, where it came from). $EXITGATE_SESSION_START > `exitgate start` marker > last handoff note > 0."""
    env = os.environ.get("EXITGATE_SESSION_START")
    if env:
        return _parse_when(env), "env"
    try:
        return float(json.loads(cfg.marker.read_text(encoding="utf-8"))["t"]), "marker"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if cfg.handoff.exists():
        return cfg.handoff.stat().st_mtime, "last handoff note"
    return 0.0, "none (run `exitgate start` at session start)"


def mark_start(cfg: Config, t: float | None = None) -> float:
    t = time.time() if t is None else t
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.marker.write_text(json.dumps({"t": t}), encoding="utf-8")
    return t


# ---------------------------------------------------------------------------------------------- checks

@dataclass
class Ctx:
    cfg: Config
    start: float

    def path(self, v: str) -> Path:
        p = Path(v)
        return p if p.is_absolute() else self.cfg.base / p


def _run(cmd, cwd: Path, timeout: float, shell: bool = False) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, shell=shell)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def check_git_dirty(spec: dict, ctx: Ctx) -> list[str]:
    """Uncommitted changes (and optionally unpushed commits) in each listed repo."""
    out = []
    for repo in spec.get("repos", ["."]):
        path = ctx.path(repo)
        args = ["git", "-C", str(path), "status", "--porcelain"]
        if not spec.get("untracked", True):
            args.append("--untracked-files=no")
        rc, text = _run(args, path if path.is_dir() else ctx.cfg.base, 60)
        if rc != 0:
            raise RuntimeError(f"{repo}: not a git repo or git failed: {text.strip()[:120]}")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if lines:
            files = ", ".join(ln[3:] for ln in lines[:4]) + (", ..." if len(lines) > 4 else "")
            out.append(f"{repo}: {len(lines)} uncommitted change(s): {files}")
        if spec.get("unpushed", False):
            rc, text = _run(["git", "-C", str(path), "rev-list", "--count", "@{u}..HEAD"], path, 60)
            if rc == 0 and text.strip() not in ("", "0"):
                out.append(f"{repo}: {text.strip()} commit(s) not pushed")
    return out


def check_scratch_unrun(spec: dict, ctx: Ctx) -> list[str]:
    """Scratch scripts written this session that never ran: the work they were for did not happen."""
    b = Bin(ctx.path(spec["dir"]) if spec.get("dir") else ctx.cfg.scratch_dir)
    return [f"scratch written, never run: {n} (scratch run {Path(n).stem})" for n in b.unrun_since(ctx.start)]


def _edited_files(roots: list[Path], ignore: set[str], skip: set[Path], since: float, limit: int) -> list[tuple[float, Path]]:
    hits, seen = [], 0
    for root in roots:
        if root.is_file():
            candidates = [(str(root.parent), [], [root.name])]
        else:
            candidates = os.walk(root)
        for dirpath, dirnames, filenames in candidates:
            dirnames[:] = [d for d in dirnames if d not in ignore]
            for f in filenames:
                seen += 1
                if seen > limit:
                    return hits
                p = Path(dirpath) / f
                if p.resolve() in skip:
                    continue
                try:
                    m = p.stat().st_mtime
                except OSError:
                    continue
                if m > since:
                    hits.append((m, p))
    return hits


def check_handoff(spec: dict, ctx: Ctx) -> list[str]:
    """Files edited this session after the last handoff note. Notes are how the next session finds the work."""
    cfg = ctx.cfg
    noted = cfg.handoff.stat().st_mtime if cfg.handoff.exists() else 0.0
    since = max(ctx.start, noted)
    ignore = set(spec.get("ignore", DEFAULT_IGNORE))
    skip = {cfg.handoff.resolve(), cfg.cache.resolve(), cfg.marker.resolve(), (cfg.scratch_dir / "_runs.json").resolve()}
    roots = [ctx.path(w) for w in spec.get("watch", ["."])]
    hits = _edited_files(roots, ignore, skip, since, int(spec.get("max_files", 50000)))
    if not hits:
        return []
    hits.sort(reverse=True)
    names = ", ".join(os.path.relpath(p, cfg.base) for _, p in hits[:4]) + (", ..." if len(hits) > 4 else "")
    return [f"{len(hits)} file(s) edited since the last handoff note ({names}): "
            f"exitgate note \"<what changed, what is open>\""]


def check_jsonl_open(spec: dict, ctx: Ctx) -> list[str]:
    """Rows in a JSONL store whose `field` equals `value` (default status == "open")."""
    path = ctx.path(spec["path"])
    fld, val = spec.get("field", "status"), spec.get("value", "open")
    show = spec.get("show", "title")
    if not path.exists():
        return [f"{path.name} missing"] if spec.get("required", False) else []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            out.append(f"{path.name} line {n} unreadable (it could be an open item)")
            continue
        if isinstance(row, dict) and row.get(fld) == val:
            out.append(str(row.get(show) or row.get("id") or f"line {n}")[:120])
    return out


def check_command(spec: dict, ctx: Ctx) -> list[str]:
    """Run a shell command; nonzero exit is a leak, its first output line is the detail."""
    cmd = spec.get("run") or spec.get("command")
    if not cmd:
        raise ConfigError("command check needs `run`")
    cwd = ctx.path(spec.get("cwd", "."))
    try:
        rc, text = _run(cmd, cwd, float(spec.get("timeout", 60)), shell=True)
    except subprocess.TimeoutExpired:
        return [f"timed out after {spec.get('timeout', 60)}s"]
    if rc == 0:
        return []
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return [f"exit {rc}" + (f": {first[:120]}" if first else "")]


CHECKS: dict[str, Callable[[dict, Ctx], list[str]]] = {
    "git_dirty": check_git_dirty,
    "scratch_unrun": check_scratch_unrun,
    "handoff": check_handoff,
    "jsonl_open": check_jsonl_open,
    "command": check_command,
}
DEFAULT_KIND = {"command": SYSTEM}
DEFAULT_LABEL = {"git_dirty": "git", "scratch_unrun": "scratch", "handoff": "handoff", "jsonl_open": "open item",
                 "command": "command"}


def register_check(name: str, fn: Callable[[dict, Ctx], list[str]], kind: str = SESSION, label: str | None = None) -> None:
    """Add a check type usable from the config: fn(spec, ctx) -> list of leak strings (empty = clean)."""
    CHECKS[name] = fn
    DEFAULT_KIND[name] = kind
    DEFAULT_LABEL[name] = label or name


# ---------------------------------------------------------------------------------------------- the gate

@dataclass
class Result:
    session: list[str] = field(default_factory=list)
    system: list[str] = field(default_factory=list)
    start: float = 0.0
    start_source: str = ""

    @property
    def safe(self) -> bool:
        return not self.session

    @property
    def line(self) -> str:
        head = "CLOSE OK" if self.safe else f"{len(self.session)} LEAK"
        return head + (f" | sys: {'; '.join(self.system[:2])}" if self.system else "")


def run_gate(cfg: Config) -> Result:
    start, src = session_start(cfg)
    ctx = Ctx(cfg, start)
    res = Result(start=start, start_source=src)
    for spec in cfg.checks:
        typ = spec["type"]
        kind = spec.get("kind", DEFAULT_KIND.get(typ, SESSION))
        if kind not in (SESSION, SYSTEM):
            raise ConfigError(f"check {typ}: kind must be session or system, not {kind!r}")
        label = spec.get("label", DEFAULT_LABEL.get(typ, typ))
        bucket = res.session if kind == SESSION else res.system
        fn = CHECKS.get(typ)
        if fn is None:
            bucket.append(f"{label}: unknown check type {typ!r}")
            continue
        try:
            bucket.extend(f"{label}: {msg}" for msg in fn(spec, ctx))
        except Exception as exc:  # noqa: BLE001  fail loud: a crashed check must never read as clean
            bucket.append(f"{label}: check failed ({type(exc).__name__}: {exc})")
    return res


def write_cache(cfg: Config, res: Result) -> None:
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.cache.write_text(json.dumps({"t": time.time(), "safe": res.safe, "line": res.line,
                                         "session": res.session, "system": res.system}), encoding="utf-8")
    except OSError:
        pass


def cached_line(cfg: Config, now: float | None = None) -> str:
    try:
        c = json.loads(cfg.cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "exit gate: not run"
    age = ((now or time.time()) - float(c.get("t", 0))) / 3600
    return c["line"] + (f" (stale {age:.0f}h)" if age > cfg.stale_after_hours else "")


def write_note(cfg: Config, text: str, when: datetime | None = None) -> Path:
    text = text.strip()
    if not text:
        raise ValueError("empty note")
    stamp = (when or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    cfg.handoff.parent.mkdir(parents=True, exist_ok=True)
    new = not cfg.handoff.exists()
    with cfg.handoff.open("a", encoding="utf-8") as fh:
        if new:
            fh.write("# Handoff\n\nNewest at the bottom. Written by `exitgate note`.\n")
        fh.write(f"\n## {stamp}\n{text}\n")
    return cfg.handoff
