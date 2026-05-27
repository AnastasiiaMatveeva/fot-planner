"""Pytest: корень tests/ в sys.path для пакета demo_business."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
