# exit-gate

Can this AI coding session be closed without losing anything? One command answers it with evidence.

```
$ python3 -m exitgate
NOT SAFE: 4 session leak(s)
  LEAK    git: .: 4 uncommitted change(s): corrections.jsonl, scratch/_runs.json, report.py, scratch/count_rows.py
  LEAK    scratch: scratch written, never run: count_rows.py (scratch run count_rows)
  LEAK    handoff: 3 file(s) edited since the last handoff note (report.py, corrections.jsonl, scratch/count_rows.py): exitgate note "<what changed, what is open>"
  LEAK    open correction: retry loop ignored the rate limit
```

## Why it exists

Long agent sessions are expensive, and they stay long for a bad reason: closing one feels like dropping
something. Sometimes it is. Edits never committed. A helper script written to fix the data and never actually
run. A correction opened mid-session and forgotten. Work the next session cannot find because nobody wrote
down what changed. Each of these was learned the hard way, and each is cheap to detect with a script.

So the decision "can I close this?" stops being a feeling and becomes an exit code.

It separates two kinds of leak on purpose:

- **Session leaks** block closing (exit 1). They exist only because this session is open and would be lost
  with it: uncommitted changes, unrun scratch scripts, edits with no handoff note, open items in a log.
- **System leaks** are reported, never blocking. A failing scheduled job or a full disk keeps leaking whether a
  session is open or not; keeping the session open does not fix it, so it should not hold the session hostage.

A check that crashes is reported as a leak, never passed silently. A gate that says "safe" because it
broke is worse than no gate.

## The pair: exit-gate + scratch

`scratch` is a bin for one-off scripts, bundled in the same package (`python3 -m exitgate scratch ...`).

The habit it supports: when an agent needs a throwaway action, it writes a file instead of an inline heredoc
or `python -c`. A file can be read, re-run for free and reviewed later; an inline snippet costs tokens every
time and leaves nothing behind. `scratch` counts runs, which gives you two signals:

- **Written, never run** is a session leak: the agent wrote the fix and never executed it, so the work it was
  for did not happen. exit-gate blocks on it.
- **Run twice or more** (`scratch promote`) means it was never a one-off. Give it a real home and a schedule.

`scratch stale` lists scripts nobody has touched in N days. Nothing is ever deleted automatically: a bin that
empties itself is a bin nobody trusts, and then the inline snippets come back.

## Install

Python 3.10+, standard library only (TOML config needs 3.11+; JSON works on 3.10).

```
pip install .            # or put the exitgate/ folder on your PYTHONPATH
python3 -m exitgate init # writes an example exitgate.toml
```

## Quick example

Real output from a fresh repo with the example config plus a `jsonl_open` check on `corrections.jsonl`:

```
$ python3 -m exitgate start
session start marked 2026-01-05 09:12:07
$ python3 -m exitgate scratch new count_rows "count rows per day"
./scratch/count_rows.py
$ echo 'print(1)' > report.py        # ...the session does its work...
$ python3 -m exitgate
NOT SAFE: 4 session leak(s)
  LEAK    git: .: 4 uncommitted change(s): corrections.jsonl, scratch/_runs.json, report.py, scratch/count_rows.py
  LEAK    scratch: scratch written, never run: count_rows.py (scratch run count_rows)
  LEAK    handoff: 3 file(s) edited since the last handoff note (report.py, corrections.jsonl, scratch/count_rows.py): exitgate note "<what changed, what is open>"
  LEAK    open correction: retry loop ignored the rate limit
$ python3 -m exitgate --cached
4 LEAK

$ python3 -m exitgate scratch run count_rows
$ git add -A && git commit -qm work   # and close the correction
$ python3 -m exitgate note "report.py added; count_rows ran once; c8 closed. Next: wire report.py into the build."
handoff written to ./HANDOFF.md
$ python3 -m exitgate
SAFE TO CLOSE
$ python3 -m exitgate --cached
CLOSE OK
```

With a system check failing, the session can still close; the status line carries it:

```
SAFE TO CLOSE
  system  jobs: exit 3: 2 jobs failing
$ python3 -m exitgate --cached
CLOSE OK | sys: jobs: exit 3: 2 jobs failing
```

## Commands

| command | does |
|---|---|
| `python3 -m exitgate` | run every check; exit 0 safe, 1 session leaks, 2 config error. `--json` for machines |
| `--cached` | print the last run's one line (`CLOSE OK`, `3 LEAK`, `... | sys: ...`), `(stale Nh)` when old |
| `note "text"` | append `## YYYY-MM-DD HH:MM` + text to the handoff file |
| `start` | record the session start time (from a session-start hook) |
| `init` | write an example `exitgate.toml` |
| `scratch new NAME "why"` | write a stub script into the bin |
| `scratch run NAME [args]` | run it and count the run |
| `scratch list` / `promote` / `stale [--days 60]` | what is there, what ran twice, what is untouched |

`--config PATH` works with every command. Default lookup: `$EXITGATE_CONFIG`, then `./exitgate.toml`, then
`./exitgate.json`, then built-in defaults (git changes in `.`, unrun scratch scripts, edits with no note).

## Config

```toml
handoff = "HANDOFF.md"      # paths are relative to this file
state_dir = "state"         # cache for --cached, session marker
scratch_dir = "scratch"
stale_after_hours = 6

[[check]]
type = "git_dirty"
repos = [".", "../shared-lib"]
untracked = true            # count untracked files
unpushed = false            # also count commits not pushed to the upstream

[[check]]
type = "scratch_unrun"      # scripts written since session start with zero runs

[[check]]
type = "handoff"
watch = ["."]               # files under here edited after the session start and after the last note
ignore = [".git", "node_modules", "state", "dist"]

[[check]]
type = "jsonl_open"         # any JSONL store: a correction log, a todo list, an incident log
path = "corrections.jsonl"
field = "status"
value = "open"
show = "title"              # which field to print
label = "open correction"
required = false            # true: a missing file is a leak

[[check]]
type = "command"            # anything else: nonzero exit = leak, first output line = detail
label = "failed services"
run = "systemctl --failed --quiet --no-legend | grep -q . && exit 1 || exit 0"
kind = "system"             # command defaults to system; every other type defaults to session
timeout = 30
```

Every check accepts `kind = "session" | "system"` and `label`. The same file works as JSON with a `"check"`
array.

### Session start

The `scratch_unrun` and `handoff` checks need to know when the session began. In order:
`$EXITGATE_SESSION_START` (epoch or ISO time), the marker written by `exitgate start`, the last handoff note's
time, or the beginning of time (so the first run asks you for a baseline note).

### Wiring it into an agent harness

Most coding agents can run a command at session start and show a command's output in a status bar. For Claude
Code, in `.claude/settings.json` (see `examples/claude-settings.json`):

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 -m exitgate start"}]}]
  },
  "statusLine": {"type": "command", "command": "python3 -m exitgate --cached"}
}
```

Then tell the agent in its instructions: before ending, run `python3 -m exitgate`; fix every LEAK; write the
handoff with `python3 -m exitgate note "..."`.

### Custom checks in Python

```python
from exitgate import Config, register_check, run_gate

def todo_markers(spec, ctx):
    hits = [p for p in ctx.path("src").rglob("*.py") if "FIXME-BEFORE-CLOSE" in p.read_text()]
    return [f"{p.name} still has a FIXME-BEFORE-CLOSE" for p in hits]

register_check("fixme", todo_markers, kind="session", label="fixme")
res = run_gate(Config.load("exitgate.toml"))     # a config entry: [[check]] type = "fixme"
print(res.safe, res.session, res.system)
```

## How it works

Each check is a function `(spec, ctx) -> list[str]`; an empty list is clean. The gate runs them in config
order, prefixes each message with the check's label, and sorts it into the session or system bucket. Any
exception becomes `label: check failed (...)` in that check's bucket. The result is printed, cached to
`state/exit_gate.json` for `--cached`, and turned into the exit code.

- `git_dirty`: `git status --porcelain` per repo; optional `git rev-list @{u}..HEAD` for unpushed commits.
  A path that is not a repo is a failed check, not a pass.
- `scratch_unrun`: reads `scratch/_runs.json`, the ledger `scratch` keeps.
- `handoff`: walks the watched trees (pruning `ignore`) for files modified after both the session start and
  the handoff file's last write. The handoff file, cache, marker and scratch ledger are excluded.
- `jsonl_open`: every row where `field == value`. A line that is not JSON is a leak; it could be an open item.
- `command`: `shell=True` in the config's directory; timeout is a leak.

## Limits

- Edits are found by file mtime. A tool that preserves mtimes, or a clock jump, can hide one; `git_dirty`
  usually catches it anyway.
- `scratch_unrun` only sees runs made through `scratch run`. Running the file directly does not count.
- The handoff check wants a note after the last edit. Edit after the note and it asks again, by design.
- One session at a time per config. Two agents sharing a working tree share one session marker.
- `command` checks run with your shell and your permissions. Treat the config file like code.

## License

MIT
