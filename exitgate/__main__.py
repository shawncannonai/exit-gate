"""exitgate: can this AI coding session be closed without losing anything?

    python3 -m exitgate                  SAFE TO CLOSE (exit 0), or the exact leaks (exit 1)
    python3 -m exitgate --json           the same, as JSON
    python3 -m exitgate --cached         one line from the last run, for a status bar
    python3 -m exitgate note "text"      append a timestamped handoff note
    python3 -m exitgate start            mark the session start (call it from a session-start hook)
    python3 -m exitgate init             write an example exitgate.toml
    python3 -m exitgate scratch ...      the one-off script bin (new | run | list | promote | stale)

Config: --config PATH, else $EXITGATE_CONFIG, else ./exitgate.toml or ./exitgate.json, else defaults
(git changes in ., unrun scratch scripts, edits with no handoff note). Exit 2 = usage or config error.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import scratch
from .core import ConfigError, Config, cached_line, mark_start, run_gate, write_cache, write_note

EXAMPLE = '''# exitgate config. Paths are relative to this file.
handoff = "HANDOFF.md"
state_dir = "state"
scratch_dir = "scratch"
stale_after_hours = 6

# Session leaks (block closing) ---------------------------------------------------

[[check]]
type = "git_dirty"
repos = ["."]
untracked = true
unpushed = false

[[check]]
type = "scratch_unrun"

[[check]]
type = "handoff"
watch = ["."]
# ignore = [".git", "node_modules", "state"]   # directory names skipped while looking for edits

# [[check]]
# type = "jsonl_open"
# path = "corrections.jsonl"
# field = "status"
# value = "open"
# show = "title"
# label = "open correction"

# System leaks (reported, never blocking) ---------------------------------------

[[check]]
type = "command"
label = "disk over 90%"
run = "python3 -c \\"import shutil,sys; u=shutil.disk_usage('.'); sys.exit(u.used/u.total>0.9)\\""
kind = "system"
'''


def _arg(args: list[str], flag: str) -> str | None:
    if flag in args:
        i = args.index(flag)
        if i + 1 >= len(args):
            raise SystemExit(f"{flag} needs a value")
        val = args[i + 1]
        del args[i:i + 2]
        return val
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    cfg_path = _arg(args, "--config")
    try:
        if args[:1] == ["init"]:
            p = Path(cfg_path or "exitgate.toml")
            if p.exists():
                print(f"{p} already exists; not touching it")
                return 2
            p.write_text(EXAMPLE, encoding="utf-8")
            print(f"wrote {p}")
            return 0
        cfg = Config.load(cfg_path)
        if args[:1] == ["scratch"]:
            return scratch.main(args[1:], directory=cfg.scratch_dir)
        if "--cached" in args:
            print(cached_line(cfg))
            return 0
        if args[:1] == ["start"]:
            t = mark_start(cfg)
            print(f"session start marked {datetime.fromtimestamp(t):%Y-%m-%d %H:%M:%S}")
            return 0
        if args[:1] == ["note"]:
            text = " ".join(args[1:]).strip()
            if not text:
                print('usage: exitgate note "<what changed, what is open>"', file=sys.stderr)
                return 2
            print(f"handoff written to {write_note(cfg, text)}")
            return 0
        if args and args[0] not in ("check", "--json"):
            print(f"unknown command {args[0]!r}\n{__doc__}", file=sys.stderr)
            return 2
        res = run_gate(cfg)
        write_cache(cfg, res)
        if "--json" in args:
            print(json.dumps({"safe": res.safe, "line": res.line, "session": res.session, "system": res.system,
                              "session_start": res.start, "session_start_from": res.start_source}, indent=1))
        else:
            print("SAFE TO CLOSE" if res.safe else f"NOT SAFE: {len(res.session)} session leak(s)")
            for s in res.session:
                print(f"  LEAK    {s}")
            for s in res.system:
                print(f"  system  {s}")
        return 0 if res.safe else 1
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
