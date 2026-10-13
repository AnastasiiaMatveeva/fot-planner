"""Стенд тестов не работает на чужой базе.

18.09.2026 набор тестов, запущенный одним процессом pytest, стёр данные живой
базы приложения: `test_contract_screen.py` внутри тестов делает `import basis`,
тот тянет `db`, и модуль базы фиксирует путь к `app/data/fot.sqlite3`. Следом
`test_plan_basis.py` поднимал `lifecycle.App` на «временной» папке, но
получал уже привязанный модуль и в `setUp` чистил таблицы живой базы.

Теперь `App` сверяет путь модуля базы со своей папкой и отказывается работать,
если они разошлись. Проверка идёт в отдельном процессе и на двух временных
папках: живая база в ней не участвует ни в каком виде.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, os.path.join(r"{root}", "app"))
    sys.path.insert(0, os.path.join(r"{root}", "harness"))
    import db                                  # путь фиксируется на папке A
    from lifecycle import App
    try:
        App(r"{other}")                        # стенд ждёт папку B
    except RuntimeError as e:
        print("ОТКАЗ:", "не к своей временной базе" in str(e))
    else:
        print("ОТКАЗА НЕТ")
""")


class StandIsolationTests(unittest.TestCase):
    def test_stand_refuses_database_bound_elsewhere(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            env = dict(os.environ, FOT_DATA_DIR=a, FOT_SKIP_ORPHANS="1",
                       PYTHONIOENCODING="utf-8")
            code = PROBE.format(root=ROOT, other=b)
            out = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(ROOT),
                                 capture_output=True, text=True, encoding="utf-8",
                                 timeout=120)
            self.assertIn("ОТКАЗ: True", out.stdout, out.stdout + out.stderr)
            # Папка B осталась без базы: стенд отказал раньше, чем её завёл.
            self.assertFalse(os.path.exists(os.path.join(b, "fot.sqlite3")))


if __name__ == "__main__":
    unittest.main()
