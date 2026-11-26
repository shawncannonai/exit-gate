"""scratch: a bin for one-off scripts, with just enough bookkeeping to be useful.

    python3 -m exitgate scratch new <name> "why"    write a stub and print its path
    python3 -m exitgate scratch run <name> [args]   run it, and count the run
    python3 -m exitgate scratch list                what is in here, and how often it ran
    python3 -m exitgate scratch promote             the ones run twice or more: not one-offs any more
    python3 -m exitgate scratch stale [--days 60]   the ones nobody has run in a long time

Why a bin needs bookkeeping: writing a one-off to a file instead of pasting it inline only pays off if the
second run happens, and the only way to know is to count. The count is also what promotes a script out of
here: run twice and it was never a one-off, it was a habit nobody noticed.

Nothing is ever deleted automatically. `stale` lists; a person deletes. A bin that empties itself is a bin
you stop trusting, and then the inline one-offs come back.

The bin directory is --dir, else $EXITGATE_SCRATCH_DIR, else ./scratch. The ledger is <dir>/_runs.json.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER_NAME = "_runs.json"
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

STUB = '''#!/usr/bin/env python3
"""{why}

Written {when} as a one-off. If this has run more than once it is not a one-off:
`scratch promote` will say so, and it wants a real home in the project it belongs to.
"""
from __future__ import annotations


def main() -> int:
    print("{name}: nothing implemented yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_dir() -> Path:
    return Path(os.environ.get("EXITGATE_SCRATCH_DIR") or "scratch")


class Bin:
    def __init__(self, directory: str | os.PathLike | None = None):
        self.dir = Path(directory) if directory else default_dir()
        self.ledger = self.dir / LEDGER_NAME

    def load(self) -> dict:
        try:
            return json.loads(self.ledger.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def save(self, d: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.ledger)

    def path_of(self, name: str) -> Path:
        if not NAME_OK.match(name) or ".." in name:
            raise ValueError(f"bad script name {name!r}: letters, digits, _ . - only")
        return self.dir / (name if name.endswith(".py") else name + ".py")

    def new(self, name: str, why: str = "") -> tuple[Path, bool]:
        """(path, created). An existing script is left alone."""
        p = self.path_of(name)
        if p.exists():
            return p, False
        self.dir.mkdir(parents=True, exist_ok=True)
        p.write_text(STUB.format(why=why or "one-off", when=now_iso()[:10], name=p.stem), encoding="utf-8")
        d = self.load()
        d[p.name] = {"why": why, "created": now_iso(), "runs": 0, "last_run": None}
        self.save(d)
        return p, True

    def run(self, name: str, args: list[str] | None = None) -> int:
        p = self.path_of(name)
        if not p.exists():
            raise FileNotFoundError(f"no such scratch script: {p}")
        d = self.load()
        row = d.setdefault(p.name, {"why": "", "created": now_iso(), "runs": 0, "last_run": None})
        row["runs"] = int(row.get("runs") or 0) + 1
        row["last_run"] = now_iso()
        self.save(d)                                   # counted before it runs: a crash still counts as a run
        return subprocess.run([sys.executable, str(p), *(args or [])]).returncode

    def rows(self) -> list[dict]:
        d = self.load()
        out = []
        for p in sorted(self.dir.glob("*.py")) if self.dir.exists() else []:
            r = d.get(p.name, {})
            out.append({"file": p.name, "why": r.get("why", ""), "runs": int(r.get("runs") or 0),
                        "created": r.get("created"), "last_run": r.get("last_run"),
                        "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")})
        return sorted(out, key=lambda r: (-r["runs"], r["file"]))

    def promote(self) -> list[dict]:
        return [r for r in self.rows() if r["runs"] >= 2]

    def stale(self, days: int = 60) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return [r for r in self.rows()
                if datetime.fromisoformat(r["last_run"] or r["modified"]) < cutoff]

    def unrun_since(self, since: float) -> list[str]:
        """Scripts created at or after `since` (epoch) that still exist and never ran through `run`."""
        out = []
        since = math.floor(since)                      # `created` is stored to the second
        for name, r in sorted(self.load().items()):
            created = datetime.fromisoformat(r["created"]).timestamp()
            if created >= since and not r.get("runs") and (self.dir / name).exists():
                out.append(name)
        return out


def main(argv: list[str] | None = None, directory: str | os.PathLike | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scratch", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None)
    sub = ap.add_subparsers(dest="cmd")
    n = sub.add_parser("new")
    n.add_argument("name")
    n.add_argument("why", nargs="?", default="")
    r = sub.add_parser("run")
    r.add_argument("name")
    r.add_argument("args", nargs=argparse.REMAINDER)
    sub.add_parser("list")
    sub.add_parser("promote")
    s = sub.add_parser("stale")
    s.add_argument("--days", type=int, default=60)
    a = ap.parse_args(argv)
    b = Bin(a.dir or directory)
    try:
        if a.cmd == "new":
            p, created = b.new(a.name, a.why)
            print(p if created else f"already here: {p}")
            return 0
        if a.cmd == "run":
            return b.run(a.name, a.args)
        if a.cmd == "promote":
            hits = b.promote()
            if not hits:
                print("  nothing here has run twice. Still one-offs.")
                return 0
            print("  These stopped being one-offs. Give them a home in the project they belong to,")
            print("  and a schedule if they have a natural clock.")
            for x in hits:
                print(f"    {x['runs']:>3} runs   {x['file']}   {x['why'][:50]}")
            return 0
        if a.cmd == "stale":
            old = b.stale(a.days)
            if not old:
                print(f"  nothing older than {a.days} days.")
                return 0
            print(f"  Untouched for {a.days}+ days. Delete by hand when you agree:")
            for x in old:
                print(f"    {x['file']}   last {(x['last_run'] or x['modified'])[:10]}")
            return 0
        rows = b.rows()
        if not rows:
            print("  empty. Write the next one-off here instead of inline.")
            return 0
        print(f"  {'runs':>4}  {'last run':<12} file")
        for x in rows:
            print(f"  {x['runs']:>4}  {(x['last_run'] or '-')[:10]:<12} {x['file']}"
                  + (f"   {x['why'][:44]}" if x["why"] else ""))
        return 0
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
