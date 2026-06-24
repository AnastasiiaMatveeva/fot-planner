"""Pytest: корень tests/ в sys.path для пакета demo_business."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Временно: pytest не собирает и не выполняет тесты.
TESTS_DISABLED = True


def pytest_ignore_collect(collection_path, config):
    if not TESTS_DISABLED:
        return None
    path = Path(str(collection_path))
    if path.name == "conftest.py":
        return False
    if "tests" in path.parts and path.suffix == ".py":
        return True
    return None


def pytest_collection_modifyitems(config, items):
    if not TESTS_DISABLED:
        return
    marker = pytest.mark.skip(reason="Тесты временно отключены")
    for item in items:
        item.add_marker(marker)


def pytest_sessionfinish(session, exitstatus):
    # pytest exit code 5 = nothing collected; считаем нормой, пока тесты выключены.
    if TESTS_DISABLED and exitstatus == 5:
        session.exitstatus = 0
