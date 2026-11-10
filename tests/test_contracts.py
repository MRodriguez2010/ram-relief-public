from __future__ import annotations

import ast
import configparser
import unittest
from pathlib import Path

from PIL import Image

import ram_relief_core as core


PROJECT = Path(__file__).resolve().parents[1]


class ReleaseContractTests(unittest.TestCase):
    def test_timer_is_non_resident_memory_only_oneshot(self) -> None:
        service = configparser.ConfigParser(interpolation=None)
        service.read(PROJECT / "systemd" / "ram-relief.service")
        timer = configparser.ConfigParser(interpolation=None)
        timer.read(PROJECT / "systemd" / "ram-relief.timer")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertIn("--auto-run", service["Service"]["ExecStart"])
        self.assertEqual(timer["Timer"]["OnUnitActiveSec"], "2min")
        self.assertEqual(timer["Timer"]["Persistent"], "false")

        tree = ast.parse((PROJECT / "ram_relief.py").read_text(encoding="utf-8"))
        auto_run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cli_auto_run")
        called_names = {
            node.func.id
            for node in ast.walk(auto_run)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertIn("run_memory_relief", called_names)
        self.assertNotIn("run_cleanup", called_names)

    def test_default_cleanup_and_policy_catalog_match_release_contract(self) -> None:
        categories = core.DEFAULT_SETTINGS["cleanup"]["categories"]
        self.assertTrue(categories["thumbnails"])
        self.assertTrue(categories["browser_cache"])
        self.assertTrue(categories["trash"])
        self.assertFalse(categories["npm"])
        self.assertFalse(categories["yarn"])
        self.assertFalse(categories["pip"])
        self.assertFalse(categories["playwright"])
        self.assertEqual(core.DEFAULT_SETTINGS["cleanup"]["stale_days"], 30)

    def test_animation_decode_budget_is_below_eight_mib(self) -> None:
        for action in ("memory", "cleanup"):
            paths = sorted((PROJECT / "assets" / "actions" / action).glob("frame-*.webp"))
            self.assertEqual(len(paths), 12)
            decoded_bytes = 0
            for path in paths:
                with Image.open(path) as image:
                    self.assertEqual(image.size, (320, 320))
                    self.assertEqual(image.format, "WEBP")
                    self.assertIn("A", image.getbands())
                    decoded_bytes += image.width * image.height * 4
            self.assertLess(decoded_bytes, 8 * 1024 * 1024)

    def test_static_action_art_is_flattened_webp(self) -> None:
        for action in ("overview", "memory", "cleanup"):
            path = PROJECT / "assets" / "actions" / action / "static.webp"
            self.assertTrue(path.is_file(), path)
            with Image.open(path) as image:
                self.assertEqual(image.size, (320, 320))
                self.assertEqual(image.format, "WEBP")
                self.assertIn("A", image.getbands())

    def test_app_icon_is_installed_by_theme_name(self) -> None:
        icon_path = PROJECT / "assets" / "ram-relief-icon.png"
        with Image.open(icon_path) as image:
            self.assertEqual(image.size, (512, 512))
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((256, 256))[3], 255)

        desktop = (PROJECT / "ram-relief.desktop").read_text(encoding="utf-8")
        ui = (PROJECT / "ram_relief_ui.py").read_text(encoding="utf-8")
        installer = (PROJECT / "install-v2").read_text(encoding="utf-8")
        self.assertIn("Icon=ram-relief", desktop)
        self.assertIn('set_icon_name("ram-relief")', ui)
        self.assertIn('icon_root/ram-relief.png', installer)

    def test_no_source_model_is_shipped(self) -> None:
        forbidden = {".blend", ".fbx", ".obj", ".gltf", ".glb", ".spline"}
        found = [path for path in (PROJECT / "assets").rglob("*") if path.suffix.lower() in forbidden]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
