"""Член рабочей группы — договор группы ему разрешён приказом (владелец
продукта 01.10.2026: «по логике, если он есть в рабочем приказе, он должен
сразу галочку ставить»). Без этого закрепление члена группы спорило с графой
«разрешенные договоры», и расчёт кончался «решения нет»."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "app"))
JS = (ROOT / "app/static/app.js").read_text(encoding="utf-8")


def build_input():
    spec = importlib.util.spec_from_file_location("build_input_access",
                                                  ROOT / "app" / "build_input.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def row(code, allowed=None, forbidden=None):
    return types.SimpleNamespace(code=code, allowed_contracts=allowed,
                                 forbidden_contracts=forbidden)


class WorkgroupAccessTests(unittest.TestCase):
    def test_group_contract_is_allowed_and_not_forbidden(self):
        got = build_input().group_contracts(
            [row("E1", "C_A", "C_B"), row("E2", None, "C_B"), row("E3", "C_A"), row("E4")],
            [{"сотрудник": "E1", "договор": "C_B"}, {"сотрудник": "E2", "договор": "C_B"},
             {"сотрудник": "E3", "договор": "C_C"}])
        self.assertEqual(got["E1"], ("C_A, C_B", None, ["C_B"]))
        self.assertEqual(got["E2"], (None, None, ["C_B"]))
        self.assertEqual(got["E3"], ("C_A, C_C", None, ["C_C"]))
        # Без группы — графы штатного как есть.
        self.assertEqual(got["E4"], (None, None, []))

    def test_registry_shows_group_contract_locked(self):
        on = JS[JS.index("function empContractsOn("):][:600]
        self.assertIn("empGroupContracts(x)", on)
        dialog = JS[JS.index("function empContractsDialog("):][:5000]
        self.assertIn('(byOrder ? " disabled" : "")', dialog)
        self.assertIn("по приказу", dialog)


if __name__ == "__main__":
    unittest.main()
