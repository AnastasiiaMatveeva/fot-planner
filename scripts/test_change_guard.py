"""Регрессии DEV-GUARD-001, без запуска приложения и живой базы."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import change_guard as G
import repo_state as S
import stop_gate
import check_results as R


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.write("app/calc.py", "value = 1\n")
        self.write("docs/ТЕСТИРОВАНИЕ.md", "DEV-GUARD-001\n")
        self.write("scripts/test_calc.py", "import unittest\nclass CalcTests(unittest.TestCase):\n    def test_old(self):\n        assert True\n")
        self.write("harness/changes.json", '{"schema_version": 1, "changes": []}\n')
        self.git("init")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args):
        return S.git(self.root, *args).decode("utf-8")

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def entry(self):
        return {"id": "BUG-001", "status": "implemented", "summary": "исправление",
                "expected": "ожидается 2", "requirement": {"path": "docs/ТЕСТИРОВАНИЕ.md", "id": "DEV-GUARD-001"},
                "files": ["app/calc.py"],
                "tests": [{"path": "scripts/test_new.py", "symbol": "NewTests.test_value"}],
                "reproduction": "Старый код: падение test_value; новый: успех"}

    def valid_change(self):
        self.write("app/calc.py", "value = 2\n")
        self.write("docs/ТЕСТИРОВАНИЕ.md", "DEV-GUARD-001\nожидается 2\n")
        self.write("scripts/test_new.py", "import unittest\nclass NewTests(unittest.TestCase):\n    def test_value(self):\n        assert 2 == 2\n")
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [self.entry()]}))

    def inspect(self):
        return G.inspect(self.root, self.base, {"scripts/test_calc.py", "scripts/test_new.py"})

    def test_clean(self):
        result = self.inspect()
        self.assertEqual(result["status"], "passed")

    def test_runtime_change_without_record(self):
        self.write("app/calc.py", "value = 2\n")
        self.assertTrue(self.inspect()["errors"])

    def test_catalogue_and_schema_changes_require_record_and_invalidate_cache(self):
        for name in ('capabilities/example/1.0.0.json', 'schemas/local-harness/input.schema.json'):
            with self.subTest(path=name):
                before = S.fingerprint(self.root, [])
                self.write(name, '{"changed": true}')
                self.assertNotEqual(before, S.fingerprint(self.root, []))
                self.assertTrue(any(name in e for e in self.inspect()['errors']))

    def test_existing_catalogue_requires_review(self):
        name = 'capabilities/example/1.0.0.json'
        self.write(name, '{"maturity": "declared"}')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'catalogue')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.write(name, '{"maturity": "qualified"}')
        self.assertIn(name, self.inspect()['review_required'])

    def test_valid_record_needs_rule_review(self):
        self.valid_change()
        result = self.inspect()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["status"], "review_required")
        self.assertIn("docs/ТЕСТИРОВАНИЕ.md", result["review_required"])

    def test_deleted_test(self):
        (self.root / "scripts/test_calc.py").unlink()
        self.assertIn("scripts/test_calc.py", self.inspect()["review_required"])

    def test_weakened_test_even_without_deleting_lines(self):
        self.write("scripts/test_calc.py", "import unittest\nclass CalcTests(unittest.TestCase):\n    def test_old(self):\n        return\n        assert True\n")
        self.assertEqual(self.inspect()["status"], "review_required")

    def test_self_approval_does_not_clear_review(self):
        self.valid_change()
        entry = self.entry()
        entry["approved"] = True
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [entry]}))
        self.assertEqual(self.inspect()["status"], "review_required")

    def test_missing_symbol(self):
        self.valid_change()
        self.write("scripts/test_new.py", "pass\n")
        self.assertTrue(self.inspect()["errors"])

    def test_existing_unchanged_test_is_not_new_regression(self):
        self.valid_change()
        entry = self.entry()
        entry["tests"] = [{"path": "scripts/test_calc.py", "symbol": "CalcTests.test_old"}]
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [entry]}))
        self.assertTrue(self.inspect()["errors"])

    def test_non_executed_test(self):
        self.valid_change()
        result = G.inspect(self.root, self.base, {"scripts/test_calc.py"})
        self.assertTrue(result["errors"])

    def test_changed_helper_not_test_body(self):
        self.valid_change()
        self.write("scripts/test_calc.py", "# new comment\n" + (self.root / "scripts/test_calc.py").read_text(encoding="utf-8"))
        entry = self.entry()
        entry["tests"] = [{"path": "scripts/test_calc.py", "symbol": "CalcTests.test_old"}]
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [entry]}))
        self.assertTrue(self.inspect()["errors"])

    def test_conflict_cannot_close(self):
        self.valid_change()
        entry = self.entry()
        entry["status"] = "conflict"
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [entry]}))
        self.assertTrue(self.inspect()["errors"])

    def test_unicode_and_spaces(self):
        self.write("app/новый файл.py", "value = 3\n")
        self.assertIn("app/новый файл.py", S.changed(self.root, self.base))

    def test_invalid_base_fails_closed(self):
        with self.assertRaises(S.RepositoryError):
            G.inspect(self.root, "missing-ref", set())

    def test_snapshot_changes_on_content_not_only_name(self):
        before = S.fingerprint(self.root, [("test", ["scripts/test_calc.py"])])
        self.write("app/calc.py", "value = 2\n")
        after = S.fingerprint(self.root, [("test", ["scripts/test_calc.py"])])
        self.assertNotEqual(before, after)

    def test_snapshot_ignores_live_data_and_secrets(self):
        before = S.fingerprint(self.root, [])
        self.write("app/data/private.txt", "do not read")
        self.write(".env", "do not read")
        self.assertEqual(before, S.fingerprint(self.root, []))

    def test_snapshot_commands_invalidate_cache(self):
        self.assertNotEqual(S.fingerprint(self.root, [("test", ["one"])]),
                            S.fingerprint(self.root, [("test", ["two"])]))

    def test_commits_do_not_hide_change_against_explicit_base(self):
        self.write("app/calc.py", "value = 2\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "change")
        self.assertTrue(self.inspect()["errors"])

    def test_deleted_record_requires_review(self):
        self.valid_change()
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "registered")
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("harness/changes.json", '{"schema_version": 1, "changes": []}')
        self.assertIn("harness/changes.json", self.inspect()["review_required"])

    def test_malformed_registry(self):
        self.write("harness/changes.json", "not json")
        self.assertEqual(self.inspect()["status"], "failed")

    def test_duplicate_ids(self):
        self.valid_change()
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [self.entry(), self.entry()]}))
        self.assertTrue(self.inspect()["errors"])

    def test_case_selector(self):
        source = b'@case("01", "regression")\ndef check_it():\n    assert True\n'
        self.assertIsNotNone(G.test_node(source, "case:01"))
        self.assertIsNone(G.test_node(source, "case:02"))

    def test_review_is_not_misreported_as_execution_error(self):
        result = R.assess(["scripts/change_guard.py"], 3,
                          "Защита изменений: review_required\n  требуется ревью: old.py\n", {})
        self.assertEqual(result["runtime_status"], "completed")
        self.assertEqual(result["acceptance_status"], "review_required")
        self.assertEqual(R.overall([result]), "review_required")

    def test_forged_review_protocol_is_error(self):
        result = R.assess(["scripts/change_guard.py"], 3, "passed", {})
        self.assertEqual(result["acceptance_status"], "error")

    def test_path_traversal_rejected(self):
        with self.assertRaises(S.RepositoryError):
            S.local_path(self.root, "../secret")

    def test_changing_protection_requests_review(self):
        for name in [".github/workflows/priyomka.yml", ".claude/settings.json",
                     "scripts/change_guard.py", "scripts/parts.py", "harness/known_red.txt"]:
            self.assertTrue(G.protected(name), name)

    def test_open_issue_does_not_block_other_fix_in_same_file(self):
        self.valid_change()
        open_issue = {**self.entry(), "id": "OTHER-001", "status": "open", "tests": []}
        self.write("harness/changes.json", json.dumps({"schema_version": 1, "changes": [open_issue, self.entry()]}))
        self.assertEqual(self.inspect()["errors"], [])


class StopCacheTests(unittest.TestCase):
    def invoke(self, previous, fingerprints):
        with patch.object(stop_gate.P, "changed_files", return_value=["app/main.py"]), \
             patch.object(stop_gate.P, "touched", return_value=[("test", ["fake.py"])]), \
             patch.object(stop_gate.S, "fingerprint", side_effect=fingerprints), \
             patch.object(stop_gate, "load", return_value=previous), \
             patch.object(stop_gate, "save") as save, \
             patch.object(stop_gate, "run", return_value={"acceptance_status": "passed", "reason": "ok"}) as run, \
             patch.object(stop_gate, "_say"), patch.object(stop_gate.sys, "stdin", io.StringIO("{}")), \
             patch.object(stop_gate.time, "time", return_value=1000):
            status = stop_gate.main()
        return status, run.call_count, save

    def test_identical_snapshot_uses_cache(self):
        status, count, _ = self.invoke({"at": 990, "ok": True, "fingerprint": "same"}, ["same"])
        self.assertEqual((status, count), (0, 0))

    def test_new_edit_within_ten_minutes_runs(self):
        status, count, _ = self.invoke({"at": 990, "ok": True, "fingerprint": "old"}, ["new", "new"])
        self.assertEqual((status, count), (0, 1))

    def test_midrun_change_is_not_cached_success(self):
        status, count, save = self.invoke({}, ["before", "after"])
        self.assertEqual((status, count), (2, 1))
        self.assertFalse(save.call_args.args[0]["ok"])

    def test_snapshot_error_blocks(self):
        status, _, _ = self.invoke({"at": 990, "ok": True}, [S.RepositoryError("git failed")])
        self.assertEqual(status, 2)

    def test_bad_cache_timestamp_runs_again(self):
        status, count, _ = self.invoke({"at": "broken", "ok": True, "fingerprint": "same"}, ["same", "same"])
        self.assertEqual((status, count), (0, 1))

    def test_future_cache_timestamp_is_not_reused(self):
        status, count, _ = self.invoke({"at": 1100, "ok": True, "fingerprint": "same"}, ["same", "same"])
        self.assertEqual((status, count), (0, 1))


if __name__ == "__main__":
    unittest.main()
