# -*- coding: utf-8 -*-
"""Самопроверка мгновенного синтаксического хука Claude Code."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lint_changed as H  # noqa: E402


def invoke(event, paths=None):
    """Вызвать обработчик с событием, не обращаясь к настоящему Git."""
    old_stdin, old_changed, old_say = sys.stdin, H._changed_paths, H._say
    messages = []
    try:
        sys.stdin = io.StringIO(json.dumps(event))
        if paths is not None:
            H._changed_paths = lambda: paths
        H._say = messages.append
        return H.main(), "".join(messages)
    finally:
        sys.stdin, H._changed_paths, H._say = old_stdin, old_changed, old_say


def main():
    settings_path = os.path.join(H.P.ROOT, ".claude", "settings.json")
    with io.open(settings_path, encoding="utf-8") as stream:
        hooks = json.load(stream)["hooks"]
    assert hooks["PostToolUse"][0]["matcher"] == "Edit|Write|Bash|PowerShell"
    assert hooks["PostToolUseFailure"][0]["matcher"] == "Bash|PowerShell"

    valid = os.path.abspath(__file__)
    code, message = invoke({"tool_name": "Edit",
                            "tool_input": {"file_path": valid}})
    assert code == 0 and not message

    handle, broken = tempfile.mkstemp(suffix=".py")
    try:
        os.write(handle, b"def broken(\n")
        os.close(handle)
        handle = -1
        for event_name in ("PostToolUse", "PostToolUseFailure"):
            code, message = invoke({"hook_event_name": event_name,
                                    "tool_name": "Bash",
                                    "tool_input": {"command": "python rewrite.py"}},
                                   [broken])
            assert code == 2
            assert "синтаксис Python" in message
    finally:
        if handle >= 0:
            os.close(handle)
        if os.path.exists(broken):
            os.unlink(broken)

    print("Хук правок: Edit, Bash и PostToolUseFailure проверены.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
