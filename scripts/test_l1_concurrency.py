"""DEV-L1-RACE-001 / DEV-L1-ARTIFACT-001: конкурентные регрессии на временных данных."""
from __future__ import annotations

import io
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "harness"))
from fot_planner.harness_local import artifacts as A


def _process_start(root, plan_id, operation, index, first_read, second_started, result):
    """Отдельный серверный процесс, собственные соединения с той же SQLite."""
    os.environ["FOT_DATA_DIR"] = root
    os.environ["FOT_SKIP_ORPHANS"] = "1"
    sys.path.insert(0, str(ROOT / "app"))
    import db as d
    import main as m
    db = d.session()
    original = m._running_run
    def delayed(session, case_id):
        run = original(session, case_id)
        if index == 0:
            first_read.set()
            second_started.wait(timeout=10)
            time.sleep(0.2)
        return run
    try:
        if index:
            if not first_read.wait(timeout=10):
                raise AssertionError("первый процесс не стартовал")
            second_started.set()
        with patch.object(m, "_running_run", side_effect=delayed):
            run, created = m._start_run(db, plan_id, {}, operation_id=operation)
        result.put({"id": run.id, "created": created})
    except Exception as exc:
        result.put({"error": repr(exc)})
    finally:
        db.close()
        d.engine.dispose()


class ArtifactConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = A.ArtifactStore(str(Path(self.temp.name) / "store"))

    def overlap(self, writer):
        # Обе записи дошли до публикации: каждая уже создала временный файл.
        barrier = threading.Barrier(2)
        first_published = threading.Event()
        lock, slots = threading.Lock(), []
        replace = A.os.replace
        def publish(src, dst):
            with lock:
                slot = len(slots)
                slots.append(src)
            barrier.wait(timeout=5)
            if slot:
                if not first_published.wait(timeout=5):
                    raise AssertionError("первая публикация не завершена")
                replace(src, dst)
            else:
                try:
                    replace(src, dst)
                finally:
                    first_published.set()
        with patch.object(A.os, "replace", side_effect=publish):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(writer) for _ in range(2)]
                values = [f.result(timeout=10) for f in futures]
        self.assertEqual(values[0], values[1])
        self.assertTrue(self.store.verify(values[0]))
        return values[0]

    def test_concurrent_same_bytes(self):
        digest = self.overlap(lambda: self.store.put_bytes(b"same input"))
        self.assertEqual(self.store.read(digest), b"same input")

    def test_concurrent_same_file(self):
        source = Path(self.temp.name) / "source.bin"
        source.write_bytes(b"same file")
        digest = self.overlap(lambda: self.store.put_file(str(source)))
        self.assertEqual(self.store.read(digest), b"same file")

    def test_existing_corrupt_artifact_is_not_accepted(self):
        digest = self.store.put_bytes(b"original")
        Path(self.store.path(digest)).write_bytes(b"corrupt")
        with self.assertRaises(ValueError):
            self.store.put_bytes(b"original")

    def test_failed_publication_cleans_only_own_temporary(self):
        with patch.object(A.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.store.put_bytes(b"data")
        self.assertEqual([p for p in Path(self.store.root).rglob("*") if p.is_file()], [])

    def test_file_digest_matches_copied_bytes(self):
        source = Path(self.temp.name) / "source.bin"
        source.write_bytes(b"first version")
        original = A.sha256_of
        def change_after_hash(path):
            digest = original(path)
            if Path(path) == source:
                source.write_bytes(b"second version")
            return digest
        with patch.object(A, "sha256_of", side_effect=change_after_hash):
            digest = self.store.put_file(str(source))
        self.assertTrue(self.store.verify(digest))


class RunConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import lifecycle
        cls.temp = tempfile.TemporaryDirectory(prefix="run_concurrency_")
        cls.env = patch.dict(os.environ)
        cls.env.start()
        cls.app = lifecycle.App(cls.temp.name)
        cls.m, cls.d = cls.app.main, cls.app.d

    @classmethod
    def tearDownClass(cls):
        cls.app.db.close()
        cls.d.engine.dispose()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.db = self.d.session()
        self.addCleanup(self.db.close)
        plan = self.d.Case(title=self.id(), year=2026)
        self.db.add(plan)
        self.db.commit()
        self.plan_id = plan.id

    def simultaneous(self, keys, settings=None):
        # Первый уже прочитал отсутствие run и задержан. Второй стартует;
        # без DB-блокировки оба читают отсутствие, с ней второй ждёт commit.
        first_read = threading.Event()
        second_started = threading.Event()
        original = self.m._running_run
        first_thread = []
        def delayed(db, plan_id):
            result = original(db, plan_id)
            if threading.get_ident() == first_thread[0]:
                first_read.set()
                second_started.wait(timeout=5)
                time.sleep(0.15)
            return result
        def worker(index):
            db = self.d.session()
            try:
                if index == 0:
                    first_thread.append(threading.get_ident())
                else:
                    if not first_read.wait(timeout=5):
                        raise AssertionError("первый запуск не дошёл до чтения")
                    second_started.set()
                run, created = self.m._start_run(db, self.plan_id,
                    (settings or [{}, {}])[index], operation_id=keys[index])
                return {"id": run.id, "created": created}
            except self.m.HTTPException as exc:
                return {"http": exc.status_code}
            finally:
                db.close()
        with patch.object(self.m, "_running_run", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(worker, 0)
                two = pool.submit(worker, 1)
                return [one.result(timeout=10), two.result(timeout=10)]

    def test_same_operation_creates_one_run(self):
        results = self.simultaneous([self.id(), self.id()])
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(sum(r["created"] for r in results), 1)
        self.assertEqual(self.db.query(self.d.Run).filter_by(case_id=self.plan_id, status="идет").count(), 1)

    def test_different_operations_still_have_one_active_run(self):
        results = self.simultaneous([self.id() + "-1", self.id() + "-2"])
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(sum(r["created"] for r in results), 1)

    def test_same_key_different_payload_is_conflict(self):
        results = self.simultaneous([self.id(), self.id()], [{"weight": 1}, {"weight": 2}])
        self.assertEqual(results[1], {"http": 409})

    def test_completed_operation_reuses_run(self):
        run, created = self.m._start_run(self.db, self.plan_id, {}, operation_id=self.id())
        run.status = "OPTIMAL"
        self.db.commit()
        again, created = self.m._start_run(self.db, self.plan_id, {}, operation_id=self.id())
        self.assertFalse(created)
        self.assertEqual(again.id, run.id)

    def test_pending_session_changes_not_lost(self):
        plan = self.db.get(self.d.Case, self.plan_id)
        plan.title = "pending edit"
        self.m._start_run(self.db, self.plan_id, {})
        self.db.expire_all()
        self.assertEqual(self.db.get(self.d.Case, self.plan_id).title, "pending edit")

    def test_fix_from_chat_does_not_queue_existing_run_again(self):
        last = self.d.Run(case_id=self.plan_id, status="OPTIMAL", settings="{}")
        busy = self.d.Run(case_id=self.plan_id, status="идет", settings="{}")
        self.db.add_all([last, busy])
        self.db.commit()
        from fastapi import BackgroundTasks
        tasks = BackgroundTasks()
        with patch.object(self.m.chat, "reply", return_value={"ok": True, "reply": "ок", "settings": {"value": 1}}), \
             patch.object(self.m.chat, "apply_fixes", return_value=([], [])), \
             patch.object(self.m.chat, "apply_settings", return_value=["условие записано"]), \
             patch.object(self.m, "_running_run", return_value=None), \
             patch.object(self.m, "_start_run", return_value=(busy, False)):
            self.m.post_message(self.plan_id, tasks, {"text": "условие"})
        self.assertEqual(tasks.tasks, [])

    def test_two_processes_share_one_reservation(self):
        context = multiprocessing.get_context("spawn")
        first_read, second_started = context.Event(), context.Event()
        queue = context.Queue()
        processes = [context.Process(target=_process_start, args=(self.temp.name, self.plan_id,
                     self.id(), i, first_read, second_started, queue)) for i in range(2)]
        try:
            for process in processes:
                process.start()
            results = [queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            self.assertFalse(any("error" in row for row in results), results)
            self.assertEqual(results[0]["id"], results[1]["id"])
            self.assertEqual(sum(r["created"] for r in results), 1)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            queue.close()
            queue.join_thread()

    def test_retry_parent_is_part_of_operation_payload(self):
        self.m._start_run(self.db, self.plan_id, {}, operation_id=self.id(), retry_of=1)
        with self.assertRaises(self.m.HTTPException) as caught:
            self.m._start_run(self.db, self.plan_id, {}, operation_id=self.id(), retry_of=2)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.query(self.d.Run).filter_by(case_id=self.plan_id).count(), 1)

    def test_reused_run_releases_writer_lock(self):
        self.m._start_run(self.db, self.plan_id, {})
        self.m._start_run(self.db, self.plan_id, {})
        other = self.d.session()
        try:
            _, created = self.m._start_run(other, self.plan_id, {})
            self.assertFalse(created)
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()
