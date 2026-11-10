from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
PRODUCTION = (
    PROJECT / "ram_relief.py",
    PROJECT / "ram_relief_core.py",
    PROJECT / "ram_relief_cleanup.py",
    PROJECT / "ram_relief_ui.py",
)


class NoSignalTests(unittest.TestCase):
    def test_no_process_signal_or_freezer_path_exists(self) -> None:
        forbidden_text = ("SIGTERM", "SIGSTOP", "SIGKILL", "cgroup.freeze", "drop_caches")
        for path in PRODUCTION:
            text = path.read_text(encoding="utf-8")
            for token in forbidden_text:
                self.assertNotIn(token, text, f"{token} found in {path.name}")
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    self.assertFalse(
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "os"
                        and node.func.attr == "kill",
                        f"os.kill call found in {path.name}",
                    )


if __name__ == "__main__":
    unittest.main()
