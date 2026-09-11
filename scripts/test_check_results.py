# -*- coding: utf-8 -*-
"""Регрессии вердикта приёмки; запускаются напрямую, независимо от tests/."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check
import check_results as R
import parts as P
import stop_gate

GOOD = "[01] ОК пример\nитого: 1 случаев, провалов 0\n"
BAD = "[01] ПРОВАЛ пример\nитого: 1 случаев, провалов 1\n"
KNOWN = {"01": ("Разобранная причина", None)}


class VerdictTests(unittest.TestCase):
    def assess(self, output=GOOD, code=0, strict=False, script="harness/crisis.py"):
        return R.assess([script], code, output, KNOWN, strict=strict)

    def test_passed(self):
        self.assertEqual(self.assess()["acceptance_status"], "passed")

    def test_known_is_failed_but_developer_compatible(self):
        result = self.assess(BAD, 1)
        self.assertEqual(result["evaluation_status"], "failed")
        self.assertEqual(result["cases"][0]["verdict"], "failed")
        self.assertEqual(result["acceptance_status"], "baseline_compatible")

    def test_strict_rejects_known(self):
        self.assertEqual(self.assess(BAD, 1, True)["acceptance_status"], "failed")

    def test_foreign_suite_cannot_use_known(self):
        self.assertEqual(self.assess(BAD, 1, script="harness/chat.py")["acceptance_status"], "failed")

    def test_new_failure(self):
        self.assertEqual(self.assess(BAD.replace("[01]", "[02]"), 1)["acceptance_status"], "failed")

    def test_crash_after_known(self):
        for out, code in [(BAD.split("итого")[0], 1), (BAD, 2),
                          (BAD + "Traceback (most recent call last):\nboom", 1)]:
            with self.subTest(out=out, code=code):
                self.assertEqual(self.assess(out, code)["acceptance_status"], "error")

    def test_zero_exit_does_not_hide_failure(self):
        self.assertEqual(self.assess(BAD, 0)["acceptance_status"], "error")

    def test_bad_summary_counts(self):
        for out in [GOOD.replace("1 случаев", "2 случаев"),
                    BAD.replace("провалов 1", "провалов 0"), GOOD + GOOD]:
            self.assertEqual(self.assess(out)["acceptance_status"], "error")

    def test_empty_case_set_not_passed(self):
        self.assertEqual(self.assess("итого: 0 случаев, провалов 0\n")["acceptance_status"], "not_run")

    def test_empty_output_not_passed(self):
        self.assertEqual(self.assess("", script="scripts/test-issues.cjs")["acceptance_status"], "not_run")

    def test_missing_executor(self):
        self.assertEqual(self.assess("", None)["runtime_status"], "error")

    def test_process_only_contract(self):
        self.assertEqual(self.assess("Assertions passed", script="scripts/test_lint_changed.py")["acceptance_status"], "passed")

    def test_overall(self):
        self.assertEqual(R.overall([]), "not_run")
        rows = [self.assess(), self.assess(BAD, 1)]
        self.assertEqual(R.overall(rows), "baseline_compatible")

    def test_no_model_when_git_fails_in_quick(self):
        with patch.object(P, "changed_files", side_effect=P.GitUnavailable("offline")), \
             patch.object(check, "run", return_value={**self.assess(), "title": "test", "duration_seconds": 0}) as run, \
             patch.object(sys, "argv", ["check.py", "--changed", "--quick"]), \
             contextlib.redirect_stdout(io.StringIO()):
            check.main()
        self.assertNotIn("harness/run.py", [call.args[1][0] for call in run.call_args_list])

    def test_empty_selection_exit_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "empty.json"
            for strict, expected in [(False, 0), (True, 1)]:
                argv = ["check.py", "--changed", "--report", str(target)] + (["--strict"] if strict else [])
                with patch.object(P, "changed_files", return_value=[]), \
                     patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check.main(), expected)
                report = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(report["acceptance_status"], "not_run")
                self.assertEqual(report["suites"], [])

    def test_report_write_error_is_nonzero(self):
        with patch.object(P, "PARTS", ()), \
             patch.object(check, "write_report", side_effect=OSError("disk full")), \
             patch.object(sys, "argv", ["check.py", "--report", "unused.json"]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check.main(), 1)

    def test_failed_atomic_replace_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "old.json"
            check.write_report(target, {"old": True})
            with patch.object(check.os, "replace", side_effect=OSError("denied")):
                with self.assertRaises(OSError):
                    check.write_report(target, {"new": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"old": True})
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_changes_select_runner_regressions(self):
        for name in ("scripts/check.py", "scripts/stop_gate.py", "harness/known_red.txt"):
            self.assertIn("вердикт приёмки", [title for title, _ in P.touched([name])])

    def test_stop_allows_only_developer_compatible_failure(self):
        for script, expected in [("harness/crisis.py", "baseline_compatible"),
                                 ("harness/chat.py", "failed")]:
            with patch.object(stop_gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, BAD)):
                self.assertEqual(stop_gate.run([script], KNOWN)["acceptance_status"], expected)

    def test_report_and_strict_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "report.json"
            for strict, expected in [(False, 0), (True, 1)]:
                argv = ["check.py", "--quick", "--report", str(target)] + (["--strict"] if strict else [])
                with patch.object(P, "PARTS", (("solver", ["harness/crisis.py"], ()),)), \
                     patch.object(P, "known_red", return_value=KNOWN), \
                     patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, BAD)), \
                     patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check.main(), expected)
                report = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(report["suites"][0]["cases"][0]["verdict"], "failed")
                self.assertEqual(len(report["suites"][0]["stdout_sha256"]), 64)
                self.assertEqual(report["exit_code"], expected)

    def test_stop_does_not_waive_crash(self):
        with patch.object(stop_gate.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, BAD + "Traceback (most recent call last):\nboom")):
            result = stop_gate.run(["harness/crisis.py"], KNOWN)
        self.assertEqual(result["acceptance_status"], "error")

    def test_real_subprocess_missing_and_success(self):
        with contextlib.redirect_stdout(io.StringIO()):
            good = check.run("python", [sys.executable, "-c", "print('assertions passed')"], {})
            missing = check.run("missing", ["fot-planner-nonexistent-executor-qa003"], {})
        self.assertEqual(good["acceptance_status"], "passed")
        self.assertEqual(missing["acceptance_status"], "error")


if __name__ == "__main__":
    unittest.main()
