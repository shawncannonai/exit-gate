import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from exitgate import Bin, Config, cached_line, mark_start, register_check, run_gate, session_start, write_cache, write_note
from exitgate.__main__ import main

GIT = ["git", "-c", "user.name=test", "-c", "user.email=you@example.com", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main"]


def git(repo, *args):
    r = subprocess.run([*GIT, "-C", str(repo), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q")
    (path / "app.py").write_text("print('hi')\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def touch(p: Path, t: float, text: str = "x\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(text)
    os.utime(p, (t, t))
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.base = Path(self._td.name)
        self._env = os.environ.pop("EXITGATE_SESSION_START", None)
        self.now = time.time()
        self.start = self.now - 1000

    def tearDown(self):
        if self._env is not None:
            os.environ["EXITGATE_SESSION_START"] = self._env
        self._td.cleanup()

    def cfg(self, checks, **raw):
        raw["check"] = checks
        c = Config.from_dict(raw, self.base)
        mark_start(c, self.start)
        return c


class GitCheck(Base):
    def test_clean_repo_is_safe(self):
        make_repo(self.base / "r")
        res = run_gate(self.cfg([{"type": "git_dirty", "repos": ["r"]}]))
        self.assertTrue(res.safe, res.session)
        self.assertEqual(res.line, "CLOSE OK")

    def test_dirty_repo_blocks(self):
        r = make_repo(self.base / "r")
        (r / "app.py").write_text("print('changed')\n")
        (r / "new.py").write_text("")
        res = run_gate(self.cfg([{"type": "git_dirty", "repos": ["r"]}]))
        self.assertFalse(res.safe)
        self.assertEqual(len(res.session), 1)
        self.assertIn("2 uncommitted change(s)", res.session[0])
        self.assertIn("app.py", res.session[0])
        res = run_gate(self.cfg([{"type": "git_dirty", "repos": ["r"], "untracked": False}]))
        self.assertIn("1 uncommitted change(s)", res.session[0])

    def test_not_a_repo_fails_loud(self):
        (self.base / "plain").mkdir()
        res = run_gate(self.cfg([{"type": "git_dirty", "repos": ["plain", "missing"]}]))
        self.assertFalse(res.safe)
        self.assertIn("check failed", res.session[0])

    def test_unpushed_commits(self):
        remote = self.base / "remote.git"
        subprocess.run([*GIT, "init", "-q", "--bare", str(remote)], check=True)
        r = make_repo(self.base / "r")
        git(r, "remote", "add", "origin", str(remote))
        git(r, "push", "-q", "-u", "origin", "main")
        spec = [{"type": "git_dirty", "repos": ["r"], "unpushed": True}]
        self.assertTrue(run_gate(self.cfg(spec)).safe)
        (r / "b.py").write_text("")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "local only")
        res = run_gate(self.cfg(spec))
        self.assertEqual(res.session, ["git: r: 1 commit(s) not pushed"])


class ScratchCheck(Base):
    def test_written_never_run_blocks_until_run(self):
        c = self.cfg([{"type": "scratch_unrun"}])
        b = Bin(c.scratch_dir)
        b.new("fix_dates", "one-off cleanup")
        res = run_gate(c)
        self.assertEqual(res.session, ["scratch: scratch written, never run: fix_dates.py (scratch run fix_dates)"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(b.run("fix_dates"), 0)
        self.assertTrue(run_gate(c).safe)

    def test_script_written_in_same_second_as_start_counts(self):
        self.start = time.time()                       # fractional start; ledger stores whole seconds
        c = self.cfg([{"type": "scratch_unrun"}])
        Bin(c.scratch_dir).new("quick")
        self.assertFalse(run_gate(c).safe)

    def test_scripts_from_earlier_sessions_do_not_block(self):
        c = self.cfg([{"type": "scratch_unrun"}])
        b = Bin(c.scratch_dir)
        b.new("old_one")
        d = b.load()
        d["old_one.py"]["created"] = datetime.fromtimestamp(self.start - 3600).astimezone().isoformat()
        b.save(d)
        self.assertTrue(run_gate(c).safe)

    def test_deleted_script_does_not_block(self):
        c = self.cfg([{"type": "scratch_unrun"}])
        p, _ = Bin(c.scratch_dir).new("gone")
        p.unlink()
        self.assertTrue(run_gate(c).safe)


class HandoffCheck(Base):
    def test_edit_without_note_blocks_note_clears(self):
        c = self.cfg([{"type": "handoff", "watch": ["."]}])
        touch(self.base / "old.py", self.start - 500)
        self.assertTrue(run_gate(c).safe)
        touch(self.base / "src" / "edited.py", self.start + 10)
        res = run_gate(c)
        self.assertFalse(res.safe)
        self.assertIn("1 file(s) edited since the last handoff note", res.session[0])
        self.assertIn("edited.py", res.session[0])
        write_note(c, "edited src/edited.py; nothing open")
        self.assertTrue(run_gate(c).safe)

    def test_edit_after_note_blocks_again(self):
        c = self.cfg([{"type": "handoff"}])
        write_note(c, "first")
        os.utime(c.handoff, (self.start + 10, self.start + 10))
        touch(self.base / "later.py", self.start + 20)
        self.assertFalse(run_gate(c).safe)

    def test_ignored_dirs_and_own_files_do_not_count(self):
        c = self.cfg([{"type": "handoff"}])
        touch(self.base / "node_modules" / "x.js", self.start + 10)
        touch(self.base / ".git" / "index", self.start + 10)
        touch(self.base / "state" / "anything.json", self.start + 10)
        write_cache(c, run_gate(c))
        self.assertTrue(run_gate(c).safe)

    def test_custom_ignore(self):
        c = self.cfg([{"type": "handoff", "ignore": ["build_out"]}])
        touch(self.base / "build_out" / "a.txt", self.start + 10)
        self.assertTrue(run_gate(c).safe)


class JsonlCheck(Base):
    def write(self, rows):
        p = self.base / "corrections.jsonl"
        p.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n")
        return p

    def test_open_rows_block(self):
        self.write([{"id": "c1", "status": "open", "title": "wrong date parser"},
                    {"id": "c2", "status": "closed", "title": "done"},
                    {"id": "c3", "status": "open"}])
        c = self.cfg([{"type": "jsonl_open", "path": "corrections.jsonl", "label": "open correction"}])
        self.assertEqual(run_gate(c).session, ["open correction: wrong date parser", "open correction: c3"])

    def test_custom_field_and_value(self):
        self.write([{"state": "todo", "name": "a"}, {"state": "done", "name": "b"}])
        c = self.cfg([{"type": "jsonl_open", "path": "corrections.jsonl", "field": "state", "value": "todo",
                       "show": "name"}])
        self.assertEqual(run_gate(c).session, ["open item: a"])

    def test_unreadable_line_is_a_leak(self):
        self.write([{"status": "closed"}, "{not json"])
        res = run_gate(self.cfg([{"type": "jsonl_open", "path": "corrections.jsonl"}]))
        self.assertIn("line 2 unreadable", res.session[0])

    def test_missing_file(self):
        self.assertTrue(run_gate(self.cfg([{"type": "jsonl_open", "path": "nope.jsonl"}])).safe)
        res = run_gate(self.cfg([{"type": "jsonl_open", "path": "nope.jsonl", "required": True}]))
        self.assertFalse(res.safe)


class CommandCheck(Base):
    py = sys.executable

    def test_zero_is_clean_nonzero_is_system_by_default(self):
        ok = {"type": "command", "run": f'"{self.py}" -c "pass"', "label": "jobs"}
        bad = {"type": "command", "run": f'"{self.py}" -c "print(\'2 jobs failing\'); raise SystemExit(3)"',
               "label": "jobs"}
        res = run_gate(self.cfg([ok, bad]))
        self.assertTrue(res.safe)
        self.assertEqual(res.system, ["jobs: exit 3: 2 jobs failing"])
        self.assertEqual(res.line, "CLOSE OK | sys: jobs: exit 3: 2 jobs failing")

    def test_session_kind_blocks(self):
        bad = {"type": "command", "run": f'"{self.py}" -c "raise SystemExit(1)"', "label": "lint", "kind": "session"}
        res = run_gate(self.cfg([bad]))
        self.assertEqual(res.session, ["lint: exit 1"])

    def test_timeout(self):
        slow = {"type": "command", "run": f'"{self.py}" -c "import time; time.sleep(5)"', "timeout": 0.3,
                "label": "slow"}
        self.assertIn("timed out", run_gate(self.cfg([slow])).system[0])

    def test_unknown_type_and_custom_check(self):
        res = run_gate(self.cfg([{"type": "nope"}]))
        self.assertIn("unknown check type", res.session[0])
        register_check("always_one", lambda spec, ctx: [f"found {spec['what']}"], label="custom")
        res = run_gate(self.cfg([{"type": "always_one", "what": "thing"}]))
        self.assertEqual(res.session, ["custom: found thing"])

    def test_crashing_custom_check_is_a_leak(self):
        def boom(spec, ctx):
            raise KeyError("ledger")
        register_check("boom", boom)
        res = run_gate(self.cfg([{"type": "boom"}]))
        self.assertFalse(res.safe)
        self.assertIn("check failed (KeyError", res.session[0])


class StateAndNotes(Base):
    def test_cached_line_and_staleness(self):
        c = self.cfg([])
        self.assertEqual(cached_line(c), "exit gate: not run")
        write_cache(c, run_gate(c))
        self.assertEqual(cached_line(c), "CLOSE OK")
        self.assertEqual(cached_line(c, now=time.time() + 7 * 3600), "CLOSE OK (stale 7h)")

    def test_note_format(self):
        c = self.cfg([])
        write_note(c, "did a thing", when=datetime(2026, 1, 2, 9, 30))
        write_note(c, "did another", when=datetime(2026, 1, 2, 10, 0))
        text = c.handoff.read_text()
        self.assertIn("\n## 2026-01-02 09:30\ndid a thing\n", text)
        self.assertTrue(text.rstrip().endswith("## 2026-01-02 10:00\ndid another"))
        with self.assertRaises(ValueError):
            write_note(c, "   ")

    def test_session_start_precedence(self):
        c = Config.from_dict({}, self.base)
        self.assertEqual(session_start(c)[0], 0.0)
        write_note(c, "x")
        os.utime(c.handoff, (100.0, 100.0))
        self.assertEqual(session_start(c), (100.0, "last handoff note"))
        mark_start(c, 200.0)
        self.assertEqual(session_start(c), (200.0, "marker"))
        os.environ["EXITGATE_SESSION_START"] = "2026-01-01T00:00:00+00:00"
        try:
            self.assertEqual(session_start(c), (1767225600.0, "env"))
        finally:
            del os.environ["EXITGATE_SESSION_START"]


class ConfigFiles(Base):
    def test_json_config_paths_relative_to_file(self):
        sub = self.base / "proj"
        sub.mkdir()
        (sub / "exitgate.json").write_text(json.dumps({"handoff": "notes/H.md", "check": []}))
        c = Config.load(sub / "exitgate.json")
        self.assertEqual(c.handoff, (sub / "notes" / "H.md").resolve())

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib is 3.11+")
    def test_toml_config(self):
        (self.base / "exitgate.toml").write_text(
            'handoff = "H.md"\n[[check]]\ntype = "command"\nrun = "exit 4"\nlabel = "x"\nkind = "session"\n')
        c = Config.load(self.base / "exitgate.toml")
        self.assertEqual(c.checks[0]["type"], "command")
        self.assertEqual(run_gate(c).session, ["x: exit 4"])

    def test_bad_check_entry(self):
        from exitgate import ConfigError
        with self.assertRaises(ConfigError):
            Config.from_dict({"check": [{"label": "no type"}]}, self.base)


class Cli(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--config", str(self.base / "exitgate.json"), *args])
        return code, out.getvalue() + err.getvalue()

    def test_exit_codes_note_and_cached(self):
        r = make_repo(self.base / "r")
        (self.base / "exitgate.json").write_text(json.dumps({"check": [
            {"type": "git_dirty", "repos": ["r"]}, {"type": "handoff", "watch": ["r"]}]}))
        c = Config.load(self.base / "exitgate.json")
        mark_start(c, self.start)
        os.utime(r / "app.py", (self.start - 100, self.start - 100))
        code, out = self.run_cli()
        self.assertEqual((code, out.strip()), (0, "SAFE TO CLOSE"))
        (r / "app.py").write_text("changed\n")
        code, out = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("NOT SAFE: 2 session leak(s)", out)
        self.assertEqual(self.run_cli("--cached")[1].strip(), "2 LEAK")
        self.assertEqual(self.run_cli("note", "changed app.py")[0], 0)
        git(r, "commit", "-qam", "change")
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_cli("--cached")[1].strip(), "CLOSE OK")

    def test_scratch_subcommand_uses_config_dir(self):
        (self.base / "exitgate.json").write_text(json.dumps({"scratch_dir": "bin", "check": []}))
        code, out = self.run_cli("scratch", "new", "probe", "check a thing")
        self.assertEqual(code, 0)
        self.assertTrue((self.base / "bin" / "probe.py").exists())

    def test_json_output(self):
        (self.base / "exitgate.json").write_text(json.dumps({"check": []}))
        code, out = self.run_cli("--json")
        self.assertEqual(json.loads(out)["safe"], True)


class ScratchBin(Base):
    def test_new_does_not_overwrite(self):
        b = Bin(self.base / "s")
        p, created = b.new("a", "why")
        self.assertTrue(created)
        self.assertIn("why", p.read_text())
        p.write_text("print('mine')\n")
        _, created = b.new("a", "again")
        self.assertFalse(created)
        self.assertEqual(p.read_text(), "print('mine')\n")
        self.assertEqual(b.load()["a.py"]["runs"], 0)

    def test_run_counts_and_passes_args_and_code(self):
        b = Bin(self.base / "s")
        p, _ = b.new("echo")
        out = self.base / "out.txt"
        p.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\nraise SystemExit(7)\n")
        self.assertEqual(b.run("echo", [str(out), "hello"]), 7)
        self.assertEqual(out.read_text(), "hello")
        self.assertEqual(b.load()["echo.py"]["runs"], 1)
        self.assertEqual(b.promote(), [])
        b.run("echo", [str(out), "again"])
        self.assertEqual([r["file"] for r in b.promote()], ["echo.py"])

    def test_stale_lists_never_deletes(self):
        b = Bin(self.base / "s")
        old, _ = b.new("old")
        b.new("fresh")
        t = time.time() - 90 * 86400
        os.utime(old, (t, t))
        self.assertEqual([r["file"] for r in b.stale(60)], ["old.py"])
        self.assertEqual(b.stale(100), [])
        self.assertTrue(old.exists())

    def test_last_run_beats_mtime_for_stale(self):
        b = Bin(self.base / "s")
        p, _ = b.new("used")
        with redirect_stdout(io.StringIO()):
            b.run("used")
        t = time.time() - 90 * 86400
        os.utime(p, (t, t))
        self.assertEqual(b.stale(60), [])

    def test_bad_names_rejected(self):
        b = Bin(self.base / "s")
        for bad in ("../x", "a/b", "", ".hidden"):
            with self.assertRaises(ValueError):
                b.path_of(bad)

    def test_run_missing(self):
        with self.assertRaises(FileNotFoundError):
            Bin(self.base / "s").run("nope")


if __name__ == "__main__":
    unittest.main()
