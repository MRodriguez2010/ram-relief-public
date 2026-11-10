#!/usr/bin/env python3
"""Guarded, manifest-driven cleanup for RAM Relief.

Only explicitly enumerated cache roots and the user's local Freedesktop Trash
are considered.  Every candidate is revalidated immediately before deletion.
"""

from __future__ import annotations

import json
import os
import stat
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ram_relief_core import HOME, append_aggregate_history


@dataclass(frozen=True)
class CleanerRoot:
    category: str
    path: Path
    browser_processes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestEntry:
    category: str
    root: str
    path: str
    device: int
    inode: int
    owner: int
    size: int
    mtime_ns: int
    ctime_ns: int
    kind: str = "file"
    original_path: str | None = None


@dataclass
class CleanupScan:
    created_at: int
    entries: tuple[ManifestEntry, ...]
    category_counts: dict[str, int]
    category_bytes: dict[str, int]
    skip_reasons: dict[str, int]

    @property
    def total_count(self) -> int:
        return sum(self.category_counts.values())

    @property
    def total_bytes(self) -> int:
        return sum(self.category_bytes.values())

    def to_aggregate_dict(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "total_count": self.total_count,
            "total_bytes": self.total_bytes,
            "category_counts": dict(self.category_counts),
            "category_bytes": dict(self.category_bytes),
            "skip_reasons": dict(self.skip_reasons),
        }


@dataclass
class CleanupResult:
    dry_run: bool
    scan: CleanupScan
    deleted_count: int = 0
    deleted_bytes: int = 0
    category_counts: dict[str, int] = field(default_factory=dict)
    category_bytes: dict[str, int] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def to_aggregate_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "scanned_count": self.scan.total_count,
            "scanned_bytes": self.scan.total_bytes,
            "deleted_count": self.deleted_count,
            "deleted_bytes": self.deleted_bytes,
            "category_counts": dict(self.category_counts),
            "category_bytes": dict(self.category_bytes),
            "skip_reasons": dict(self.skip_reasons),
        }


FIXED_ROOTS: tuple[CleanerRoot, ...] = (
    CleanerRoot("thumbnails", HOME / ".cache/thumbnails"),
    CleanerRoot("browser_cache", HOME / ".cache/zen", ("zen",)),
    CleanerRoot("browser_cache", HOME / ".cache/mozilla/firefox", ("firefox",)),
    CleanerRoot("browser_cache", HOME / ".cache/google-chrome", ("chrome", "google-chrome")),
    CleanerRoot("browser_cache", HOME / ".cache/BraveSoftware/Brave-Browser", ("brave",)),
    CleanerRoot("browser_cache", HOME / ".cache/chromium", ("chromium",)),
    CleanerRoot("npm", HOME / ".npm/_cacache"),
    CleanerRoot("yarn", HOME / ".cache/yarn/v6"),
    CleanerRoot("yarn", HOME / ".cache/yarn/berry"),
    CleanerRoot("pip", HOME / ".cache/pip/http-v2"),
    CleanerRoot("pip", HOME / ".cache/pip/wheels"),
    CleanerRoot("playwright", HOME / ".cache/ms-playwright"),
)

HARD_PROTECTED_ROOTS: tuple[Path, ...] = (
    HOME / ".cache/codex-runtimes",
    HOME / ".cache/codex-update-manager",
    HOME / ".cache/google-chrome-debug",
    HOME / ".config",
    HOME / ".local/share",
    HOME / ".mozilla",
    HOME / ".ssh",
    HOME / ".gnupg",
    Path("/tmp"),
    Path("/var"),
    Path("/media"),
    Path("/mnt"),
    Path("/run/user") / str(os.getuid()) / "gvfs",
)


def _is_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def normalize_exclusions(values: Iterable[str]) -> tuple[Path, ...]:
    result: list[Path] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        expanded = Path(os.path.abspath(os.path.expanduser(raw.strip())))
        if expanded not in result:
            result.append(expanded)
    return tuple(result)


def is_excluded(path: Path, exclusions: Iterable[Path]) -> bool:
    absolute = Path(os.path.abspath(path))
    return any(absolute == exclusion or _is_below(absolute, exclusion) for exclusion in exclusions)


def _root_is_permitted(root: Path) -> bool:
    absolute = Path(os.path.abspath(root))
    if not _is_below(absolute, HOME):
        return False
    return not any(absolute == protected or _is_below(absolute, protected) for protected in HARD_PROTECTED_ROOTS)


def running_process_names() -> set[str]:
    names: set[str] = set()
    proc = Path("/proc")
    for child in proc.iterdir():
        if not child.name.isdigit():
            continue
        try:
            name = (child / "comm").read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if name:
            names.add(name)
    return names


def browser_is_stopped(root: CleanerRoot, running: set[str]) -> bool:
    if not root.browser_processes:
        return True
    for name in running:
        if any(token == name or token in name for token in root.browser_processes):
            return False
    return True


def open_file_identities() -> set[tuple[int, int]]:
    identities: set[tuple[int, int]] = set()
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        fd_dir = proc_dir / "fd"
        try:
            descriptors = list(fd_dir.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                info = descriptor.stat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                identities.add((info.st_dev, info.st_ino))
    return identities


def _scan_root(
    root_spec: CleanerRoot,
    *,
    cutoff: float,
    exclusions: tuple[Path, ...],
    open_files: set[tuple[int, int]],
    skips: Counter[str],
) -> list[ManifestEntry]:
    root = root_spec.path
    if not _root_is_permitted(root):
        skips["protected or out-of-scope root"] += 1
        return []
    try:
        if root.is_symlink():
            skips["symlink root"] += 1
            return []
        root_info = root.stat(follow_symlinks=False)
        resolved = root.resolve(strict=True)
    except OSError:
        return []
    if resolved != root.absolute():
        skips["unresolved root"] += 1
        return []
    if is_excluded(root, exclusions):
        skips["excluded"] += 1
        return []

    entries: list[ManifestEntry] = []
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        safe_directories: list[str] = []
        for name in directory_names:
            child = directory_path / name
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                skips["disappeared"] += 1
                continue
            if stat.S_ISLNK(info.st_mode):
                skips["symlink"] += 1
            elif info.st_dev != root_info.st_dev:
                skips["filesystem boundary"] += 1
            elif is_excluded(child, exclusions):
                skips["excluded"] += 1
            else:
                safe_directories.append(name)
        directory_names[:] = safe_directories

        for name in file_names:
            path = directory_path / name
            if is_excluded(path, exclusions):
                skips["excluded"] += 1
                continue
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                skips["disappeared"] += 1
                continue
            if not stat.S_ISREG(info.st_mode):
                skips["not a regular file"] += 1
                continue
            if info.st_dev != root_info.st_dev:
                skips["filesystem boundary"] += 1
                continue
            if info.st_uid != os.getuid():
                skips["different owner"] += 1
                continue
            if info.st_mtime > cutoff:
                skips["fresh"] += 1
                continue
            if (info.st_dev, info.st_ino) in open_files:
                skips["open file"] += 1
                continue
            entries.append(
                ManifestEntry(
                    category=root_spec.category,
                    root=str(root),
                    path=str(path),
                    device=info.st_dev,
                    inode=info.st_ino,
                    owner=info.st_uid,
                    size=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                    ctime_ns=info.st_ctime_ns,
                )
            )
    return entries


def _gio() -> tuple[Any, Any] | tuple[None, None]:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib

        return Gio, GLib
    except (ImportError, ValueError):
        return None, None


TRASH_ATTRIBUTES = ",".join(
    (
        "standard::name",
        "standard::type",
        "standard::size",
        "trash::orig-path",
        "time::modified",
        "time::modified-usec",
        "time::changed",
        "time::changed-usec",
        "unix::device",
        "unix::inode",
        "unix::uid",
    )
)


def _info_uint64(info: Any, name: str) -> int:
    try:
        return int(info.get_attribute_uint64(name))
    except (AttributeError, TypeError, ValueError):
        return 0


def _info_uint32(info: Any, name: str) -> int:
    try:
        return int(info.get_attribute_uint32(name))
    except (AttributeError, TypeError, ValueError):
        return 0


def _time_ns(info: Any, base: str) -> int:
    return _info_uint64(info, base) * 1_000_000_000 + _info_uint32(info, f"{base}-usec") * 1_000


def _validated_trash_origin(original: object, exclusions: tuple[Path, ...]) -> tuple[Path | None, str | None]:
    if isinstance(original, bytes):
        original = original.decode("utf-8", errors="surrogateescape")
    if not isinstance(original, str) or not os.path.isabs(original):
        return None, "malformed Trash metadata"
    original_path = Path(os.path.abspath(original))
    if not _is_below(original_path, HOME):
        return None, "non-local Trash origin"
    if any(original_path == protected or _is_below(original_path, protected) for protected in HARD_PROTECTED_ROOTS):
        return None, "protected Trash origin"
    if is_excluded(original_path, exclusions):
        return None, "excluded"
    return original_path, None


def _append_trash_tree(
    item: Any,
    info: Any,
    original_path: Path,
    *,
    exclusions: tuple[Path, ...],
    open_files: set[tuple[int, int]],
    skips: Counter[str],
    entries: list[ManifestEntry],
    Gio: Any,
) -> None:
    file_type = info.get_file_type()
    if file_type == Gio.FileType.SYMBOLIC_LINK:
        skips["symlink"] += 1
        return
    if file_type not in (Gio.FileType.REGULAR, Gio.FileType.DIRECTORY):
        skips["Trash special item"] += 1
        return
    if is_excluded(original_path, exclusions):
        skips["excluded"] += 1
        return
    device = _info_uint32(info, "unix::device")
    inode = _info_uint64(info, "unix::inode")
    owner = _info_uint32(info, "unix::uid")
    if not device or not inode or owner != os.getuid():
        skips["unverifiable Trash item"] += 1
        return

    if file_type == Gio.FileType.DIRECTORY:
        try:
            enumerator = item.enumerate_children(
                TRASH_ATTRIBUTES,
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS,
                None,
            )
        except Exception:
            skips["unreadable Trash directory"] += 1
            return
        try:
            while True:
                child_info = enumerator.next_file(None)
                if child_info is None:
                    break
                child_name = child_info.get_name()
                if not child_name or child_name in (".", "..") or "/" in child_name:
                    skips["malformed Trash metadata"] += 1
                    continue
                child = item.get_child(child_name)
                _append_trash_tree(
                    child,
                    child_info,
                    original_path / child_name,
                    exclusions=exclusions,
                    open_files=open_files,
                    skips=skips,
                    entries=entries,
                    Gio=Gio,
                )
        finally:
            try:
                enumerator.close(None)
            except Exception:
                pass
        kind = "trash_dir"
    else:
        if (device, inode) in open_files:
            skips["open file"] += 1
            return
        kind = "trash"

    entries.append(
        ManifestEntry(
            category="trash",
            root="trash:///",
            path=item.get_uri(),
            device=device,
            inode=inode,
            owner=owner,
            size=int(info.get_size()),
            mtime_ns=_time_ns(info, "time::modified"),
            ctime_ns=_time_ns(info, "time::changed"),
            kind=kind,
            original_path=str(original_path),
        )
    )


def _scan_trash(exclusions: tuple[Path, ...], open_files: set[tuple[int, int]], skips: Counter[str]) -> list[ManifestEntry]:
    Gio, _GLib = _gio()
    if Gio is None:
        skips["Trash API unavailable"] += 1
        return []
    trash = Gio.File.new_for_uri("trash:///")
    try:
        enumerator = trash.enumerate_children(TRASH_ATTRIBUTES, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
    except Exception:
        skips["Trash API unavailable"] += 1
        return []
    entries: list[ManifestEntry] = []
    try:
        while True:
            info = enumerator.next_file(None)
            if info is None:
                break
            name = info.get_name()
            original = info.get_attribute_byte_string("trash::orig-path")
            original_path, invalid_reason = _validated_trash_origin(original, exclusions)
            if not name or original_path is None:
                skips[invalid_reason or "malformed Trash metadata"] += 1
                continue
            child = trash.get_child(name)
            _append_trash_tree(
                child,
                info,
                original_path,
                exclusions=exclusions,
                open_files=open_files,
                skips=skips,
                entries=entries,
                Gio=Gio,
            )
    finally:
        try:
            enumerator.close(None)
        except Exception:
            pass
    return entries


def scan_cleanup(settings: Mapping[str, Any]) -> CleanupScan:
    cleanup = settings["cleanup"]
    enabled = cleanup["categories"]
    cutoff = time.time() - int(cleanup["stale_days"]) * 86400
    exclusions = normalize_exclusions(cleanup.get("exclusions", []))
    running = running_process_names()
    open_files = open_file_identities()
    skips: Counter[str] = Counter()
    entries: list[ManifestEntry] = []

    for root in FIXED_ROOTS:
        if not enabled.get(root.category, False):
            continue
        if root.browser_processes and not browser_is_stopped(root, running):
            skips[f"{root.path.name} browser is running"] += 1
            continue
        entries.extend(
            _scan_root(root, cutoff=cutoff, exclusions=exclusions, open_files=open_files, skips=skips)
        )
    if enabled.get("trash", False) and cleanup.get("automatic_trash", True):
        entries.extend(_scan_trash(exclusions, open_files, skips))

    entries.sort(
        key=lambda item: (
            item.category,
            -item.path.count("/") if item.kind in ("trash", "trash_dir") else 0,
            item.path,
        )
    )
    counts: Counter[str] = Counter(item.category for item in entries)
    sizes: Counter[str] = Counter()
    for item in entries:
        sizes[item.category] += item.size
    return CleanupScan(
        created_at=int(time.time()),
        entries=tuple(entries),
        category_counts=dict(counts),
        category_bytes=dict(sizes),
        skip_reasons=dict(skips),
    )


def _matches_manifest_stat(entry: ManifestEntry, info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_dev == entry.device
        and info.st_ino == entry.inode
        and info.st_uid == entry.owner
        and info.st_size == entry.size
        and info.st_mtime_ns == entry.mtime_ns
        and info.st_ctime_ns == entry.ctime_ns
    )


def _delete_file_entry(entry: ManifestEntry, open_files: set[tuple[int, int]]) -> tuple[bool, str]:
    root = Path(entry.root)
    path = Path(entry.path)
    if not _root_is_permitted(root) or root.is_symlink():
        return False, "protected or changed root"
    try:
        resolved_root = root.resolve(strict=True)
        relative = path.relative_to(root)
    except (OSError, ValueError):
        return False, "path left fixed root"
    if resolved_root != root.absolute() or not relative.parts:
        return False, "unresolved root"

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(resolved_root, flags)
    except OSError:
        return False, "root unavailable"
    try:
        for component in relative.parts[:-1]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError:
                return False, "parent changed or became symlink"
            os.close(descriptor)
            descriptor = next_descriptor
        name = relative.parts[-1]
        try:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError:
            return False, "file disappeared"
        if not _matches_manifest_stat(entry, current):
            return False, "manifest revalidation failed"
        if (current.st_dev, current.st_ino) in open_files:
            return False, "open file"
        try:
            os.unlink(name, dir_fd=descriptor)
        except OSError:
            return False, "delete failed"
        return True, "deleted"
    finally:
        os.close(descriptor)


def _trash_info_matches(entry: ManifestEntry, info: Any) -> bool:
    Gio, _GLib = _gio()
    expected_type = None
    if Gio is not None:
        expected_type = Gio.FileType.DIRECTORY if entry.kind == "trash_dir" else Gio.FileType.REGULAR
    return (
        (expected_type is None or info.get_file_type() == expected_type)
        and
        _info_uint32(info, "unix::device") == entry.device
        and _info_uint64(info, "unix::inode") == entry.inode
        and _info_uint32(info, "unix::uid") == entry.owner
        and int(info.get_size()) == entry.size
        and _time_ns(info, "time::modified") == entry.mtime_ns
        and _time_ns(info, "time::changed") == entry.ctime_ns
    )


def _delete_trash_entry(entry: ManifestEntry, open_files: set[tuple[int, int]]) -> tuple[bool, str]:
    Gio, _GLib = _gio()
    if Gio is None or not entry.path.startswith("trash:///"):
        return False, "Trash API unavailable"
    if entry.kind == "trash" and (entry.device, entry.inode) in open_files:
        return False, "open file"
    item = Gio.File.new_for_uri(entry.path)
    try:
        info = item.query_info(TRASH_ATTRIBUTES, Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, None)
    except Exception:
        return False, "Trash item disappeared"
    if not _trash_info_matches(entry, info):
        return False, "manifest revalidation failed"
    original = info.get_attribute_byte_string("trash::orig-path")
    if isinstance(original, bytes):
        original = original.decode("utf-8", errors="surrogateescape")
    if original and original != entry.original_path:
        return False, "Trash origin changed"
    try:
        if not item.delete(None):
            return False, "Trash delete failed"
    except Exception:
        return False, "Trash delete failed"
    return True, "deleted"


def execute_cleanup(scan: CleanupScan, *, dry_run: bool) -> CleanupResult:
    result = CleanupResult(dry_run=dry_run, scan=scan)
    if dry_run:
        result.category_counts = dict(scan.category_counts)
        result.category_bytes = dict(scan.category_bytes)
        return result

    open_files = open_file_identities()
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    skips: Counter[str] = Counter()
    for entry in scan.entries:
        if entry.kind in ("trash", "trash_dir"):
            deleted, reason = _delete_trash_entry(entry, open_files)
        else:
            deleted, reason = _delete_file_entry(entry, open_files)
        if deleted:
            result.deleted_count += 1
            result.deleted_bytes += entry.size
            counts[entry.category] += 1
            sizes[entry.category] += entry.size
        else:
            skips[reason] += 1
    result.category_counts = dict(counts)
    result.category_bytes = dict(sizes)
    result.skip_reasons = dict(skips)
    return result


def run_cleanup(settings: Mapping[str, Any], *, dry_run: bool = False) -> CleanupResult:
    scan = scan_cleanup(settings)
    result = execute_cleanup(scan, dry_run=dry_run)
    if not dry_run:
        append_aggregate_history({"kind": "cleanup", **result.to_aggregate_dict()})
    return result


def scan_json(settings: Mapping[str, Any]) -> str:
    return json.dumps(scan_cleanup(settings).to_aggregate_dict(), indent=2, sort_keys=True)


__all__ = [
    "CleanupResult",
    "CleanupScan",
    "CleanerRoot",
    "FIXED_ROOTS",
    "HARD_PROTECTED_ROOTS",
    "ManifestEntry",
    "browser_is_stopped",
    "execute_cleanup",
    "is_excluded",
    "normalize_exclusions",
    "open_file_identities",
    "run_cleanup",
    "running_process_names",
    "scan_cleanup",
    "scan_json",
]
