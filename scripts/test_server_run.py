"""Сервис запускается на Linux-сервере (DEV-DEPLOY-001).

Путь к решателю был зашит как `.venv/Scripts/fot-planner.exe` — на Linux
расчёт не стартовал бы вовсе. База работает в журнале WAL, чтобы несколько
пользователей и фоновые потоки реже упирались в блокировку; восстановление из
копии убирает журнал прежней базы. Живая база в проверке не участвует.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]


def solver_exe(root, executable):
    """`_solver_exe` из app/main.py без импорта всего приложения."""
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "_solver_exe")
    space = {"os": os, "ROOT": str(root),
             "sys": types.SimpleNamespace(executable=str(executable))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), space)
    return space["_solver_exe"]()


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


class ServerRunTests(unittest.TestCase):
    def test_solver_found_in_linux_venv(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            exe = touch(root / ".venv" / "bin" / "fot-planner")
            self.assertEqual(solver_exe(root, root / "py" / "python"), exe)

    def test_solver_found_next_to_interpreter_without_venv(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            name = "fot-planner.exe" if os.name == "nt" else "fot-planner"
            exe = touch(root / "usr" / "bin" / name)
            self.assertEqual(solver_exe(root / "app", root / "usr" / "bin" / "python"), exe)

    def test_database_opens_in_wal(self):
        probe = textwrap.dedent("""
            import os, sys
            sys.path.insert(0, os.path.join(r"{root}", "app"))
            import db
            with db.engine.connect() as c:
                print("MODE", c.exec_driver_sql("PRAGMA journal_mode").scalar())
        """).format(root=ROOT)
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ, FOT_DATA_DIR=d, PYTHONIOENCODING="utf-8")
            out = subprocess.run([sys.executable, "-c", probe], env=env, cwd=str(ROOT),
                                 capture_output=True, text=True, encoding="utf-8",
                                 timeout=120)
            self.assertIn("MODE wal", out.stdout, out.stdout + out.stderr)

    def test_restore_drops_journal_of_previous_database(self):
        with tempfile.TemporaryDirectory() as d:
            db, copy = os.path.join(d, "fot.sqlite3"), os.path.join(d, "copy.bak")
            conn = sqlite3.connect(copy)
            conn.execute("CREATE TABLE t (x)")
            conn.execute("INSERT INTO t VALUES (7)")
            conn.commit()
            conn.close()
            for name in (db, db + "-wal", db + "-shm"):
                Path(name).write_bytes(b"stale")
            out = subprocess.run([sys.executable, str(ROOT / "scripts" / "migrate.py"),
                                  "--db", db, "--restore", copy],
                                 capture_output=True, text=True, encoding="utf-8",
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=120)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertFalse(os.path.exists(db + "-wal"))
            self.assertFalse(os.path.exists(db + "-shm"))
            conn = sqlite3.connect(db)
            self.assertEqual(conn.execute("SELECT x FROM t").fetchall(), [(7,)])
            conn.close()


if __name__ == "__main__":
    unittest.main()
