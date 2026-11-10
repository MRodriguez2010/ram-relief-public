from __future__ import annotations

import os
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

import ram_relief_cleanup as cleanup


class CleanupSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.root = Path(self.temp.name)
        self.old = time.time() - 45 * 86400
        self.cutoff = time.time() - 30 * 86400

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _file(self, relative: str, content: bytes = b"cache", *, old: bool = True) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if old:
            os.utime(path, (self.old, self.old))
        return path

    def _scan(self, exclusions: tuple[Path, ...] = (), open_files: set[tuple[int, int]] | None = None):
        skips: Counter[str] = Counter()
        entries = cleanup._scan_root(
            cleanup.CleanerRoot("thumbnails", self.root),
            cutoff=self.cutoff,
            exclusions=exclusions,
            open_files=open_files or set(),
            skips=skips,
        )
        return entries, skips

    def test_stale_file_is_manifested_and_fresh_file_is_not(self) -> None:
        self._file("old.png")
        self._file("fresh.png", old=False)
        entries, skips = self._scan()
        self.assertEqual([Path(item.path).name for item in entries], ["old.png"])
        self.assertEqual(skips["fresh"], 1)

    def test_symlink_escape_is_never_followed(self) -> None:
        outside = self._file("outside.txt")
        link = self.root / "escape"
        link.symlink_to(Path("/etc"), target_is_directory=True)
        entries, skips = self._scan()
        self.assertEqual([Path(item.path).name for item in entries], [outside.name])
        self.assertGreaterEqual(skips["symlink"], 1)

    def test_exclusion_only_reduces_scope(self) -> None:
        kept = self._file("excluded/keep.cache")
        removed = self._file("allowed/remove.cache")
        entries, skips = self._scan((self.root / "excluded",))
        self.assertEqual([Path(item.path) for item in entries], [removed])
        self.assertTrue(kept.exists())
        self.assertGreaterEqual(skips["excluded"], 1)

    def test_open_file_is_skipped(self) -> None:
        path = self._file("open.cache")
        with path.open("rb"):
            info = path.stat()
            entries, skips = self._scan(open_files={(info.st_dev, info.st_ino)})
        self.assertEqual(entries, [])
        self.assertEqual(skips["open file"], 1)

    def test_changed_inode_fails_revalidation(self) -> None:
        path = self._file("changed.cache")
        entries, _skips = self._scan()
        path.unlink()
        replacement = self._file("changed.cache", b"new")
        scan = cleanup.CleanupScan(int(time.time()), tuple(entries), {"thumbnails": 1}, {"thumbnails": 5}, {})
        result = cleanup.execute_cleanup(scan, dry_run=False)
        self.assertEqual(result.deleted_count, 0)
        self.assertTrue(replacement.exists())
        self.assertEqual(result.skip_reasons["manifest revalidation failed"], 1)

    def test_renamed_file_is_skipped(self) -> None:
        path = self._file("rename.cache")
        entries, _skips = self._scan()
        renamed = path.with_name("renamed.cache")
        path.rename(renamed)
        scan = cleanup.CleanupScan(int(time.time()), tuple(entries), {"thumbnails": 1}, {"thumbnails": 5}, {})
        result = cleanup.execute_cleanup(scan, dry_run=False)
        self.assertEqual(result.deleted_count, 0)
        self.assertTrue(renamed.exists())
        self.assertEqual(result.skip_reasons["file disappeared"], 1)

    def test_parent_symlink_swap_cannot_escape(self) -> None:
        path = self._file("parent/item.cache")
        entries, _skips = self._scan()
        original = self.root / "parent-original"
        (self.root / "parent").rename(original)
        (self.root / "parent").symlink_to(Path("/etc"), target_is_directory=True)
        scan = cleanup.CleanupScan(int(time.time()), tuple(entries), {"thumbnails": 1}, {"thumbnails": 5}, {})
        result = cleanup.execute_cleanup(scan, dry_run=False)
        self.assertEqual(result.deleted_count, 0)
        self.assertTrue((original / path.name).exists())
        self.assertEqual(result.skip_reasons["parent changed or became symlink"], 1)

    def test_valid_manifest_deletes_exact_file(self) -> None:
        path = self._file("delete.cache")
        entries, _skips = self._scan()
        scan = cleanup.CleanupScan(int(time.time()), tuple(entries), {"thumbnails": 1}, {"thumbnails": path.stat().st_size}, {})
        result = cleanup.execute_cleanup(scan, dry_run=False)
        self.assertEqual(result.deleted_count, 1)
        self.assertFalse(path.exists())

    def test_aggregate_output_contains_no_filenames(self) -> None:
        path = self._file("private-name.cache")
        entries, _skips = self._scan()
        scan = cleanup.CleanupScan(int(time.time()), tuple(entries), {"thumbnails": 1}, {"thumbnails": path.stat().st_size}, {})
        aggregate = cleanup.execute_cleanup(scan, dry_run=True).to_aggregate_dict()
        self.assertNotIn("private-name", str(aggregate))
        self.assertNotIn("path", aggregate)

    def test_browser_running_check_is_fail_closed(self) -> None:
        spec = cleanup.CleanerRoot("browser_cache", self.root, ("zen",))
        self.assertFalse(cleanup.browser_is_stopped(spec, {"zen"}))
        self.assertTrue(cleanup.browser_is_stopped(spec, {"firefox"}))

    def test_malformed_and_nonlocal_trash_origins_are_rejected(self) -> None:
        path, reason = cleanup._validated_trash_origin("relative/path", ())
        self.assertIsNone(path)
        self.assertEqual(reason, "malformed Trash metadata")
        path, reason = cleanup._validated_trash_origin("/media/external/file", ())
        self.assertIsNone(path)
        self.assertEqual(reason, "non-local Trash origin")

    def test_trash_origin_exclusion_is_respected(self) -> None:
        excluded = Path.home() / "Documents" / "keep"
        path, reason = cleanup._validated_trash_origin(str(excluded / "file"), (excluded,))
        self.assertIsNone(path)
        self.assertEqual(reason, "excluded")

    def test_protected_configuration_trash_origin_is_rejected(self) -> None:
        path, reason = cleanup._validated_trash_origin(str(Path.home() / ".config" / "important.conf"), ())
        self.assertIsNone(path)
        self.assertEqual(reason, "protected Trash origin")


if __name__ == "__main__":
    unittest.main()
