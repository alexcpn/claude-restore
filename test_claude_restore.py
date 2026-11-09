import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_restore as cr


class RestoreTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / "claude"
        (self.claude / "sessions").mkdir(parents=True)
        (self.claude / "projects/p").mkdir(parents=True)
        self.work = self.root / "my work"
        self.work.mkdir()
        patches = [
            mock.patch.object(cr, "CLAUDE_DIR", self.claude),
            mock.patch.object(cr, "STATE_DIR", self.root / "state"),
            mock.patch.object(cr, "SNAPSHOTS", self.root / "state/snapshots"),
            mock.patch.object(cr, "boot_id", side_effect=lambda: self.boot),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.boot = "boot1"

    def add_live(self, sid, pid=None, kind="interactive", transcript=True):
        pid = pid or os.getpid()
        start = cr.proc_start(pid)
        data = dict(pid=pid, procStart=start, sessionId=sid, cwd=str(self.work),
                    name=sid, kind=kind, entrypoint="cli",
                    startedAt=len(list((self.claude / "sessions").iterdir())))
        path = self.claude / "sessions" / f"{sid}.json"
        path.write_text(json.dumps(data))
        if transcript:
            (self.claude / "projects/p" / f"{sid}.jsonl").write_text("{}")
        return path

    def test_stale_and_background_sessions_are_ignored(self):
        path = self.add_live("stale")
        data = json.loads(path.read_text())
        data["procStart"] = "1"
        path.write_text(json.dumps(data))
        self.add_live("bg", kind="background")
        self.add_live("good")
        self.assertEqual([s["sessionId"] for s in cr.live_sessions()], ["good"])

    def test_new_boot_snapshot_does_not_hide_previous_boot(self):
        path = self.add_live("a")
        cr.snapshot()
        path.unlink()
        self.boot = "boot2"
        cr.snapshot()  # the timer fires at login with nothing running
        _, data = cr.previous_snapshot()
        self.assertEqual([s["sessionId"] for s in data["sessions"]], ["a"])

    def test_grace_period_keeps_then_drops_vanished_session(self):
        path = self.add_live("a", pid=1)
        with mock.patch.object(cr, "proc_start", return_value=json.loads(path.read_text())["procStart"]):
            cr.snapshot()
        path.unlink()
        self.assertEqual(len(cr.snapshot()), 1)
        future = time.time() + cr.GRACE_SECONDS + 1
        with mock.patch.object(cr.time, "time", return_value=future):
            self.assertEqual(cr.snapshot(), [])

    def test_same_pid_with_new_id_replaces_old_session(self):
        path = self.add_live("old")
        cr.snapshot()
        path.unlink()
        self.add_live("new")
        self.assertEqual([s["sessionId"] for s in cr.snapshot()], ["new"])

    def restore_from_boot1(self, *sessions, **kwargs):
        paths = [self.add_live(s) for s in sessions]
        cr.snapshot()
        for path in paths:
            path.unlink()
        self.boot = "boot2"
        with mock.patch.object(cr.subprocess, "run") as run:
            result = cr.restore(**kwargs)
        return result, run

    def test_restore_opens_all_tabs_in_one_new_window(self):
        result, run = self.restore_from_boot1("a", "b")
        self.assertEqual(result, 0)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertEqual((argv[1], argv[5]), ("--window", "--tab"))
        self.assertEqual(argv[2], f"--working-directory={self.work}")
        self.assertIn("--resume a;", argv[4])
        self.assertIn("--resume b;", argv[8])

    def test_restore_runs_only_once(self):
        self.restore_from_boot1("a")
        with mock.patch.object(cr.subprocess, "run") as run:
            cr.restore()
        run.assert_not_called()
        with mock.patch.object(cr.subprocess, "run") as run:
            cr.restore(force=True)
        run.assert_called_once()

    def test_dry_run_opens_nothing_and_does_not_mark(self):
        _, run = self.restore_from_boot1("a", dry_run=True)
        run.assert_not_called()
        self.assertNotIn("restoredAt", cr.previous_snapshot()[1])

    def test_skips_running_missing_dir_and_empty_sessions(self):
        self.add_live("running")
        self.add_live("empty", transcript=False)
        gone = self.add_live("gone")
        data = json.loads(gone.read_text())
        data["cwd"] = str(self.root / "deleted")
        gone.write_text(json.dumps(data))
        cr.snapshot()
        (self.claude / "sessions/empty.json").unlink()
        gone.unlink()
        self.boot = "boot2"
        with mock.patch.object(cr.subprocess, "run") as run:
            cr.restore()
        run.assert_not_called()

    def test_terminal_failure_is_reported(self):
        path = self.add_live("a")
        cr.snapshot()
        path.unlink()
        self.boot = "boot2"
        error = subprocess.CalledProcessError(1, "gnome-terminal")
        with mock.patch.object(cr.subprocess, "run", side_effect=error):
            self.assertEqual(cr.restore(), 1)

    def add_transcript(self, sid, age=0, entrypoint="cli", cwd=None, title="T"):
        path = self.claude / "projects/p" / f"{sid}.jsonl"
        user = dict(type="user", cwd=cwd or str(self.work), entrypoint=entrypoint)
        path.write_text(json.dumps(user) + "\n"
                        + json.dumps(dict(type="ai-title", aiTitle=title)) + "\n")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    def test_recent_lists_newest_stopped_sessions_up_to_limit(self):
        for index in range(cr.PICKER_LIMIT + 2):
            self.add_transcript(f"s{index}", age=index * 10)
        self.add_live("running")
        self.add_transcript("running", age=-5)
        self.add_transcript("headless", age=-5, entrypoint="sdk-cli")
        self.add_transcript("gone", age=-5, cwd=str(self.root / "deleted"))
        ids = [s["sessionId"] for s in cr.recent_sessions()]
        self.assertEqual(ids, [f"s{i}" for i in range(cr.PICKER_LIMIT)])

    def test_title_cannot_break_picker_rows(self):
        self.add_transcript("a", title="--two\nlines")
        done = subprocess.CompletedProcess([], 1, stdout="")
        with mock.patch.object(cr.subprocess, "run", return_value=done) as run:
            cr.pick()
        self.assertNotIn("--two", " ".join(run.call_args.args[0]))
        self.assertEqual(len(run.call_args.kwargs["input"].splitlines()), 5)

    def test_pick_reopens_only_ticked_sessions(self):
        self.add_transcript("a")
        self.add_transcript("b")
        calls = [subprocess.CompletedProcess([], 0, stdout="b\n"), None]
        with mock.patch.object(cr.subprocess, "run", side_effect=calls) as run:
            self.assertEqual(cr.pick(), 0)
        script = run.call_args_list[1].args[0][4]
        self.assertIn("--resume b;", script)
        self.assertNotIn("--resume a;", script)

    def test_pick_cancel_opens_nothing(self):
        self.add_transcript("a")
        done = subprocess.CompletedProcess([], 1, stdout="")
        with mock.patch.object(cr.subprocess, "run", return_value=done) as run:
            self.assertEqual(cr.pick(), 0)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
