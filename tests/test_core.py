from __future__ import annotations

import hashlib
import errno
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import ram_relief_core as core


def make_scope(root: Path, name: str, *, memory: int = 300 * core.MIB, swap: int = 0, cpu: int = 1000, io: int = 1000) -> Path:
    scope = root / name
    scope.mkdir()
    (scope / "memory.current").write_text(f"{memory}\n", encoding="utf-8")
    (scope / "memory.swap.current").write_text(f"{swap}\n", encoding="utf-8")
    (scope / "memory.reclaim").write_text("", encoding="utf-8")
    (scope / "cpu.stat").write_text(f"usage_usec {cpu}\n", encoding="utf-8")
    (scope / "io.stat").write_text(f"8:0 rbytes={io // 2} wbytes={io - io // 2}\n", encoding="utf-8")
    return scope


class SettingsTests(unittest.TestCase):
    def test_legacy_approvals_are_discarded(self) -> None:
        settings, migrated = core.normalize_settings(
            {"approved": ["zen", "firefox"], "only_under_pressure": False, "show_results": False}
        )
        self.assertTrue(migrated)
        self.assertFalse(settings["setup_complete"])
        self.assertEqual(settings["schema_version"], 2)
        self.assertEqual(settings["app_policies"]["zen"], core.POLICY_AUTO)
        self.assertFalse(settings["display"]["show_results"])
        self.assertNotIn("approved", settings)

    def test_malformed_settings_fail_to_setup_required(self) -> None:
        settings, migrated = core.normalize_settings(["bad"])
        self.assertTrue(migrated)
        self.assertFalse(settings["setup_complete"])

    def test_protected_policy_cannot_be_overridden(self) -> None:
        raw = dict(core.DEFAULT_SETTINGS)
        raw = json.loads(json.dumps(raw))
        raw["app_policies"]["chrome"] = core.POLICY_AUTO
        settings, migrated = core.normalize_settings(raw)
        self.assertFalse(migrated)
        self.assertEqual(settings["app_policies"]["chrome"], core.POLICY_NEVER)


class PressureTests(unittest.TestCase):
    def test_available_or_psi_triggers(self) -> None:
        settings = json.loads(json.dumps(core.DEFAULT_SETTINGS))
        low = core.MemorySnapshot(1000, 199, 500, 500, 0.0)
        psi = core.MemorySnapshot(1000, 500, 500, 500, 2.0)
        comfortable = core.MemorySnapshot(1000, 500, 500, 500, 1.99)
        self.assertTrue(core.pressure_triggered(low, settings))
        self.assertTrue(core.pressure_triggered(psi, settings))
        self.assertFalse(core.pressure_triggered(comfortable, settings))

    def test_swap_reserve_is_greater_of_fixed_or_percent(self) -> None:
        settings = json.loads(json.dumps(core.DEFAULT_SETTINGS))
        snapshot = core.MemorySnapshot(8 * core.GIB, core.GIB, 16 * core.GIB, 10 * core.GIB, 3.0)
        self.assertEqual(core._swap_reclaim_budget(snapshot, settings), 6 * core.GIB)


class CgroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)
        self.settings = json.loads(json.dumps(core.DEFAULT_SETTINGS))
        self.settings["setup_complete"] = True

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_only_scope_units_are_discovered(self) -> None:
        make_scope(self.root, "app-zen.scope")
        service = make_scope(self.root, "app-zen.service")
        apps = core.discover_app_cgroups(self.settings, cgroup_root=self.root)
        self.assertEqual([item.unit for item in apps], ["app-zen.scope"])
        self.assertTrue(service.exists())

    def test_unknown_and_chromium_scopes_are_protected(self) -> None:
        make_scope(self.root, "app-unfamiliar.scope")
        make_scope(self.root, r"app-org.chromium.Chromium-abc.scope")
        apps = core.discover_app_cgroups(self.settings, cgroup_root=self.root)
        self.assertEqual(len(apps), 2)
        self.assertTrue(all(item.policy == core.POLICY_NEVER for item in apps))
        self.assertTrue(all(item.protected_reason for item in apps))

    def test_write_reclaim_accepts_known_user_scope(self) -> None:
        scope = make_scope(self.root, "app-zen.scope")
        app = core.discover_app_cgroups(self.settings, cgroup_root=self.root)[0]
        core.write_memory_reclaim(app, 64 * core.MIB, self.root)
        self.assertTrue((scope / "memory.reclaim").read_text(encoding="utf-8").startswith(str(64 * core.MIB)))
        self.assertIn("swappiness=max", (scope / "memory.reclaim").read_text(encoding="utf-8"))

    def test_write_reclaim_refuses_scope_outside_fixed_root(self) -> None:
        other = Path(tempfile.mkdtemp(dir=Path.home()))
        try:
            scope = make_scope(other, "app-zen.scope")
            reading = core.read_cgroup(scope)
            app = core.AppCgroup("app-zen.scope", scope, "zen", "Zen Browser", core.POLICY_AUTO, None, reading, "key")
            with self.assertRaises(PermissionError):
                core.write_memory_reclaim(app, 64 * core.MIB, self.root)
        finally:
            for child in scope.iterdir():
                child.unlink()
            scope.rmdir()
            other.rmdir()

    def test_idle_history_and_playback_protection(self) -> None:
        make_scope(self.root, "app-zen.scope", cpu=5000, io=2000)
        app = core.discover_app_cgroups(self.settings, cgroup_root=self.root)[0]
        activity_path = self.root / "activity.json"
        core.atomic_write_json(
            activity_path,
            {
                "version": 1,
                "samples": {
                    app.key: {
                        "app_id": "zen",
                        "timestamp": time.time() - 700,
                        "cpu_usec": 5000,
                        "io_bytes": 2000,
                        "memory_current": 300 * core.MIB,
                        "idle_since": time.time() - 700,
                    }
                },
                "cooldowns": {},
                "reclaims": {},
            },
        )
        with mock.patch.object(core, "active_playback_tokens", return_value=set()), mock.patch.object(
            core, "active_inhibitor_units", return_value=set()
        ):
            _apps, decisions, _activity = core.assess_cgroups(
                self.settings,
                automatic=True,
                cgroup_root=self.root,
                activity_path=activity_path,
            )
        self.assertTrue(decisions[0].eligible)
        self.assertEqual(decisions[0].status, "Idle")

        with mock.patch.object(core, "active_playback_tokens", return_value={"zen"}), mock.patch.object(
            core, "active_inhibitor_units", return_value=set()
        ):
            _apps, decisions, _activity = core.assess_cgroups(
                self.settings,
                automatic=True,
                cgroup_root=self.root,
                activity_path=activity_path,
            )
        self.assertFalse(decisions[0].eligible)
        self.assertEqual(decisions[0].status, "Playing")

    def test_refault_extends_cooldown(self) -> None:
        make_scope(self.root, "app-zen.scope", memory=500 * core.MIB)
        app = core.discover_app_cgroups(self.settings, cgroup_root=self.root)[0]
        activity_path = self.root / "activity.json"
        core.atomic_write_json(
            activity_path,
            {
                "version": 1,
                "samples": {},
                "cooldowns": {},
                "reclaims": {
                    app.key: {
                        "app_id": "zen",
                        "timestamp": time.time() - 60,
                        "after_memory": 300 * core.MIB,
                        "actual_reduction": 200 * core.MIB,
                    }
                },
            },
        )
        with mock.patch.object(core, "active_playback_tokens", return_value=set()), mock.patch.object(
            core, "active_inhibitor_units", return_value=set()
        ):
            _apps, decisions, activity = core.assess_cgroups(
                self.settings,
                automatic=True,
                cgroup_root=self.root,
                activity_path=activity_path,
            )
        self.assertEqual(decisions[0].status, "Recently restored")
        self.assertGreater(activity["cooldowns"][app.key], time.time() + 55 * 60)

    def test_disappearing_cgroup_is_skipped(self) -> None:
        make_scope(self.root, "app-zen.scope")
        with mock.patch.object(core, "read_cgroup", side_effect=FileNotFoundError):
            self.assertEqual(core.discover_app_cgroups(self.settings, cgroup_root=self.root), [])

    def test_partial_eagain_is_measured_without_retry(self) -> None:
        scope = make_scope(self.root, "app-zen.scope", memory=400 * core.MIB)
        app = core.discover_app_cgroups(self.settings, cgroup_root=self.root)[0]
        decision = core.AppDecision(app.unit, "zen", "Zen Browser", core.POLICY_AUTO, "Idle", 400 * core.MIB, True, "Eligible")
        before = core.MemorySnapshot(8 * core.GIB, core.GIB, 16 * core.GIB, 10 * core.GIB, 3.0)
        after = core.MemorySnapshot(8 * core.GIB, int(1.5 * core.GIB), 16 * core.GIB, int(9.9 * core.GIB), 2.5)
        readings = [
            core.CgroupReading(400 * core.MIB, 0, 1000, 0),
            core.CgroupReading(300 * core.MIB, 100 * core.MIB, 1000, 0),
        ]
        with mock.patch.object(core, "read_memory_snapshot", side_effect=[before, after, after]), mock.patch.object(
            core, "reclaim_supported", return_value=(True, "Available")
        ), mock.patch.object(
            core, "assess_cgroups", return_value=([app], [decision], {"samples": {}, "cooldowns": {}, "reclaims": {}})
        ), mock.patch.object(core, "read_cgroup", side_effect=readings), mock.patch.object(
            core, "write_memory_reclaim", side_effect=OSError(errno.EAGAIN, "partial reclaim")
        ) as writer, mock.patch.object(core, "save_activity"):
            result = core.run_memory_relief(self.settings, automatic=False, cgroup_root=self.root)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(result.parked[0].memory_reduction_bytes, 100 * core.MIB)
        self.assertIn("not retried", result.stopped_reason)


if __name__ == "__main__":
    unittest.main()
