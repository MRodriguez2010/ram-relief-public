#!/usr/bin/env python3
"""Safe, user-only memory parking primitives for RAM Relief.

The production engine writes only to cgroup v2 ``memory.reclaim`` for known,
user-owned graphical application scopes.  It does not send process signals,
freeze cgroups, drop caches, stop units, or restart applications.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


APP_ID = "io.github.RamRelief"
APP_NAME = "RAM Relief"
APP_SUBTITLE = "Memory & Storage Care"
SCHEMA_VERSION = 2

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "ram-relief"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local/state")) / "ram-relief"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
ACTIVITY_PATH = STATE_DIR / "activity.json"
HISTORY_PATH = STATE_DIR / "history.json"

POLICY_AUTO = "auto"
POLICY_MANUAL = "manual"
POLICY_NEVER = "never"
POLICY_LABELS = {
    POLICY_AUTO: "Auto",
    POLICY_MANUAL: "Allow manually",
    POLICY_NEVER: "Never park",
}

MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True)
class AppSpec:
    app_id: str
    label: str
    matchers: tuple[str, ...]
    default_policy: str
    protected_reason: str | None = None


APP_CATALOG: tuple[AppSpec, ...] = (
    AppSpec("zen", "Zen Browser", ("zen", "app.zen-browser.zen"), POLICY_AUTO),
    AppSpec("firefox", "Firefox", ("firefox", "org.mozilla.firefox"), POLICY_AUTO),
    AppSpec("brave", "Brave Browser", ("brave", "com.brave.browser"), POLICY_AUTO),
    AppSpec("discord", "Discord", ("discord", "com.discordapp.discord"), POLICY_AUTO),
    AppSpec("slack", "Slack", ("slack", "com.slack.slack"), POLICY_AUTO),
    AppSpec("teams", "Teams for Linux", ("teams-for-linux", "teams_for_linux", "com.github.ismaelmartinez.teams_for_linux"), POLICY_AUTO),
    AppSpec("telegram", "Telegram Desktop", ("telegram", "org.telegram.desktop"), POLICY_AUTO),
    AppSpec("obsidian", "Obsidian", ("obsidian", "md.obsidian.obsidian"), POLICY_AUTO),
    AppSpec("spotify", "Spotify", ("spotify", "com.spotify.client"), POLICY_MANUAL),
    AppSpec("steam", "Steam", ("steam", "com.valvesoftware.steam"), POLICY_MANUAL),
    AppSpec("thunderbird", "Thunderbird", ("thunderbird", "org.mozilla.thunderbird"), POLICY_MANUAL),
    AppSpec("vlc", "VLC media player", ("vlc", "org.videolan.vlc"), POLICY_MANUAL),
    AppSpec("vscode", "Visual Studio Code", ("visual-studio-code", "visual_studio_code", "com.visualstudio.code", "code-url-handler"), POLICY_MANUAL),
    AppSpec("chrome", "Google Chrome", ("google-chrome", "com.google.chrome"), POLICY_NEVER, "Always protected browser"),
    AppSpec("chromium", "Chromium", ("chromium", "org.chromium.chromium"), POLICY_NEVER, "Always protected browser"),
    AppSpec("codex", "Codex / ChatGPT", ("codex-desktop", "codex_desktop", "chatgpt", "com.openai"), POLICY_NEVER, "Always protected workspace"),
)

APP_BY_ID = {spec.app_id: spec for spec in APP_CATALOG}

HARD_PROTECT_MATCHERS: tuple[tuple[str, str], ...] = (
    ("google-chrome", "Chrome is always protected"),
    ("com.google.chrome", "Chrome is always protected"),
    ("chromium", "Chromium is always protected"),
    ("codex", "Codex and ChatGPT are always protected"),
    ("chatgpt", "Codex and ChatGPT are always protected"),
    ("gnome-terminal", "Terminals are always protected"),
    ("org.gnome.console", "Terminals are always protected"),
    ("kgx", "Terminals are always protected"),
    ("ptyxis", "Terminals are always protected"),
    ("konsole", "Terminals are always protected"),
    ("xterm", "Terminals are always protected"),
    ("nautilus", "File managers are always protected"),
    ("thunar", "File managers are always protected"),
    ("nemo", "File managers are always protected"),
    ("dolphin", "File managers are always protected"),
    ("rustdesk", "Remote desktop tools are always protected"),
    ("anydesk", "Remote desktop tools are always protected"),
    ("teamviewer", "Remote desktop tools are always protected"),
    ("dropbox", "Sync and backup clients are always protected"),
    ("nextcloud", "Sync and backup clients are always protected"),
    ("syncthing", "Sync and backup clients are always protected"),
    ("deja-dup", "Sync and backup clients are always protected"),
    ("restic", "Sync and backup clients are always protected"),
    ("keepass", "Password managers are always protected"),
    ("bitwarden", "Password managers are always protected"),
    ("1password", "Password managers are always protected"),
    ("proton-vpn", "VPN clients are always protected"),
    ("openvpn", "VPN clients are always protected"),
    ("wireguard", "VPN clients are always protected"),
    ("networkmanager", "Desktop and session infrastructure is protected"),
    ("gnome-shell", "Desktop and session infrastructure is protected"),
    ("gnome-session", "Desktop and session infrastructure is protected"),
    ("xdg-desktop", "Desktop and session infrastructure is protected"),
    ("pipewire", "Desktop and session infrastructure is protected"),
    ("wireplumber", "Desktop and session infrastructure is protected"),
    ("dbus", "Desktop and session infrastructure is protected"),
    ("ram-relief", "RAM Relief protects itself"),
)


DEFAULT_SETTINGS: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "setup_complete": False,
    "memory_mode": "pressure-aware",
    "app_policies": {spec.app_id: spec.default_policy for spec in APP_CATALOG},
    "memory": {
        "available_trigger_percent": 20.0,
        "available_stop_percent": 30.0,
        "psi_some_avg10_trigger": 2.0,
        "idle_minutes": 10,
        "cpu_percent_max": 1.0,
        "io_mib_per_sample_max": 4.0,
        "minimum_candidate_mib": 192,
        "reclaim_percent": 35.0,
        "minimum_reclaim_mib": 64,
        "maximum_reclaim_mib": 512,
        "run_cap_percent_ram": 20.0,
        "swap_reserve_gib": 2.0,
        "swap_reserve_percent": 25.0,
        "cooldown_minutes": 30,
        "refault_cooldown_minutes": 60,
    },
    "cleanup": {
        "categories": {
            "thumbnails": True,
            "browser_cache": True,
            "trash": True,
            "npm": False,
            "yarn": False,
            "pip": False,
            "playwright": False,
        },
        "stale_days": 30,
        "automatic_trash": True,
        "exclusions": [],
    },
    "theme": "system",
    "reduced_motion": "system",
    "display": {"show_results": True},
}


def _deep_merge(base: dict[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _valid_policy(app_id: str, value: object) -> str:
    spec = APP_BY_ID[app_id]
    if spec.protected_reason:
        return POLICY_NEVER
    if value in (POLICY_AUTO, POLICY_MANUAL, POLICY_NEVER):
        return str(value)
    return spec.default_policy


def normalize_settings(raw: object) -> tuple[dict[str, Any], bool]:
    """Return a validated v2 document and whether a legacy migration occurred."""
    migrated = not isinstance(raw, Mapping) or raw.get("schema_version") != SCHEMA_VERSION
    if migrated:
        settings = copy.deepcopy(DEFAULT_SETTINGS)
        if isinstance(raw, Mapping):
            display = raw.get("show_results")
            if isinstance(display, bool):
                settings["display"]["show_results"] = display
        return settings, True

    settings = _deep_merge(DEFAULT_SETTINGS, raw)
    settings["schema_version"] = SCHEMA_VERSION
    settings["setup_complete"] = bool(settings.get("setup_complete", False))
    if settings.get("memory_mode") not in ("pressure-aware", "manual-only", "off"):
        settings["memory_mode"] = "pressure-aware"
    policies = settings.get("app_policies")
    if not isinstance(policies, Mapping):
        policies = {}
    settings["app_policies"] = {
        app_id: _valid_policy(app_id, policies.get(app_id)) for app_id in APP_BY_ID
    }
    cleanup = settings["cleanup"]
    categories = cleanup.get("categories")
    if not isinstance(categories, Mapping):
        categories = {}
    cleanup["categories"] = {
        key: bool(categories.get(key, enabled))
        for key, enabled in DEFAULT_SETTINGS["cleanup"]["categories"].items()
    }
    try:
        cleanup["stale_days"] = max(1, min(365, int(cleanup.get("stale_days", 30))))
    except (TypeError, ValueError):
        cleanup["stale_days"] = 30
    cleanup["automatic_trash"] = bool(cleanup.get("automatic_trash", True))
    exclusions = cleanup.get("exclusions")
    cleanup["exclusions"] = [str(item) for item in exclusions if isinstance(item, str)] if isinstance(exclusions, list) else []
    if settings.get("theme") not in ("system", "dark", "light"):
        settings["theme"] = "system"
    if settings.get("reduced_motion") not in ("system", "reduce", "full"):
        settings["reduced_motion"] = "system"
    settings["display"]["show_results"] = bool(settings["display"].get("show_results", True))
    return settings, False


def atomic_write_json(path: Path, data: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class SettingsStore:
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = path
        self.data = copy.deepcopy(DEFAULT_SETTINGS)
        self.migrated = False

    def load(self) -> dict[str, Any]:
        raw: object = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            raw = {}
        self.data, self.migrated = normalize_settings(raw)
        return self.data

    def save(self) -> None:
        normalized, _ = normalize_settings(self.data)
        self.data = normalized
        atomic_write_json(self.path, self.data)


@dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int
    available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    psi_some_avg10: float

    @property
    def available_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return 100.0 * self.available_bytes / self.total_bytes

    @property
    def swap_used_bytes(self) -> int:
        return max(0, self.swap_total_bytes - self.swap_free_bytes)


def _read_key_values(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].isdigit():
            multiplier = 1024 if len(fields) >= 3 and fields[2].lower() == "kb" else 1
            values[fields[0].rstrip(":")] = int(fields[1]) * multiplier
    return values


def read_memory_snapshot(
    meminfo_path: Path = Path("/proc/meminfo"),
    psi_path: Path = Path("/proc/pressure/memory"),
) -> MemorySnapshot:
    info = _read_key_values(meminfo_path)
    psi_avg10 = 0.0
    try:
        for line in psi_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("some "):
                match = re.search(r"\bavg10=([0-9.]+)", line)
                if match:
                    psi_avg10 = float(match.group(1))
                break
    except OSError:
        psi_avg10 = 0.0
    return MemorySnapshot(
        total_bytes=info.get("MemTotal", 0),
        available_bytes=info.get("MemAvailable", 0),
        swap_total_bytes=info.get("SwapTotal", 0),
        swap_free_bytes=info.get("SwapFree", 0),
        psi_some_avg10=psi_avg10,
    )


def pressure_triggered(snapshot: MemorySnapshot, settings: Mapping[str, Any]) -> bool:
    memory = settings["memory"]
    return (
        snapshot.available_percent < float(memory["available_trigger_percent"])
        or snapshot.psi_some_avg10 >= float(memory["psi_some_avg10_trigger"])
    )


def format_bytes(value: int, *, compact: bool = True) -> str:
    size = float(max(0, value))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    unit = units[0]
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    precision = 1 if compact else 2
    return f"{size:.{precision}f} {unit}"


def decode_systemd_unit(value: str) -> str:
    return re.sub(
        r"\\x([0-9A-Fa-f]{2})",
        lambda match: bytes.fromhex(match.group(1)).decode("latin-1"),
        value,
    )


def default_cgroup_root(uid: int | None = None) -> Path:
    user_id = os.getuid() if uid is None else uid
    return Path(f"/sys/fs/cgroup/user.slice/user-{user_id}.slice/user@{user_id}.service/app.slice")


@dataclass(frozen=True)
class CgroupReading:
    memory_current: int
    swap_current: int
    cpu_usec: int
    io_bytes: int


@dataclass(frozen=True)
class AppCgroup:
    unit: str
    path: Path
    app_id: str | None
    label: str
    policy: str
    protected_reason: str | None
    reading: CgroupReading
    key: str


@dataclass(frozen=True)
class AppDecision:
    unit: str
    app_id: str | None
    label: str
    policy: str
    status: str
    memory_current: int
    eligible: bool
    reason: str


@dataclass(frozen=True)
class ParkedApp:
    app_id: str
    label: str
    requested_bytes: int
    memory_reduction_bytes: int
    cgroup_swap_growth_bytes: int


@dataclass
class MemoryRunResult:
    mode: str
    automatic: bool
    dry_run: bool
    pressure_triggered: bool
    before: MemorySnapshot
    after: MemorySnapshot
    decisions: list[AppDecision] = field(default_factory=list)
    parked: list[ParkedApp] = field(default_factory=list)
    error: str | None = None
    stopped_reason: str | None = None

    @property
    def available_change_bytes(self) -> int:
        return self.after.available_bytes - self.before.available_bytes

    @property
    def swap_growth_bytes(self) -> int:
        return self.after.swap_used_bytes - self.before.swap_used_bytes

    @property
    def actual_memory_reduction_bytes(self) -> int:
        return sum(item.memory_reduction_bytes for item in self.parked)

    def to_aggregate_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "automatic": self.automatic,
            "dry_run": self.dry_run,
            "pressure_triggered": self.pressure_triggered,
            "before": asdict(self.before),
            "after": asdict(self.after),
            "available_change_bytes": self.available_change_bytes,
            "swap_growth_bytes": self.swap_growth_bytes,
            "actual_memory_reduction_bytes": self.actual_memory_reduction_bytes,
            "parked": [asdict(item) for item in self.parked],
            "skipped": [
                {"app_id": item.app_id, "label": item.label, "status": item.status, "reason": item.reason}
                for item in self.decisions
                if not item.eligible
            ],
            "error": self.error,
            "stopped_reason": self.stopped_reason,
        }


def _read_int(path: Path) -> int:
    value = path.read_text(encoding="utf-8").strip()
    return 0 if value == "max" else int(value)


def read_cgroup(path: Path) -> CgroupReading:
    cpu_usec = 0
    for line in (path / "cpu.stat").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "usage_usec":
            cpu_usec = int(fields[1])
            break
    io_bytes = 0
    try:
        for line in (path / "io.stat").read_text(encoding="utf-8").splitlines():
            for name, value in re.findall(r"\b(rbytes|wbytes)=(\d+)", line):
                if name in ("rbytes", "wbytes"):
                    io_bytes += int(value)
    except OSError:
        io_bytes = 0
    try:
        swap_current = _read_int(path / "memory.swap.current")
    except OSError:
        swap_current = 0
    return CgroupReading(
        memory_current=_read_int(path / "memory.current"),
        swap_current=swap_current,
        cpu_usec=cpu_usec,
        io_bytes=io_bytes,
    )


def _scope_match_text(unit: str) -> str:
    return decode_systemd_unit(unit).lower().replace("_", "-")


def classify_scope(unit: str, policies: Mapping[str, str]) -> tuple[str | None, str, str, str | None]:
    text = _scope_match_text(unit)
    for matcher, reason in HARD_PROTECT_MATCHERS:
        if matcher in text:
            matching = next((spec for spec in APP_CATALOG if any(token in text for token in spec.matchers)), None)
            return (
                matching.app_id if matching else None,
                matching.label if matching else decode_systemd_unit(unit.removesuffix(".scope")),
                POLICY_NEVER,
                reason,
            )
    for spec in APP_CATALOG:
        if any(token in text for token in spec.matchers):
            policy = _valid_policy(spec.app_id, policies.get(spec.app_id))
            return spec.app_id, spec.label, policy, spec.protected_reason
    return None, decode_systemd_unit(unit.removesuffix(".scope")), POLICY_NEVER, "Unknown applications are always protected"


def discover_app_cgroups(
    settings: Mapping[str, Any],
    cgroup_root: Path | None = None,
    uid: int | None = None,
) -> list[AppCgroup]:
    user_id = os.getuid() if uid is None else uid
    root = default_cgroup_root(user_id) if cgroup_root is None else cgroup_root
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return []
    result: list[AppCgroup] = []
    for path in sorted(resolved_root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.name.endswith(".scope") or not path.is_dir() or path.is_symlink():
            continue
        try:
            if path.parent != resolved_root or path.stat(follow_symlinks=False).st_uid != user_id:
                continue
            reading = read_cgroup(path)
        except (OSError, ValueError):
            continue
        app_id, label, policy, protected_reason = classify_scope(path.name, settings["app_policies"])
        key = hashlib.sha256(path.name.encode("utf-8", errors="surrogateescape")).hexdigest()[:20]
        result.append(
            AppCgroup(
                unit=path.name,
                path=path,
                app_id=app_id,
                label=label,
                policy=policy,
                protected_reason=protected_reason,
                reading=reading,
                key=key,
            )
        )
    return result


def _run_text(command: list[str], *, timeout: float = 4.0) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def active_playback_tokens() -> set[str]:
    tokens: set[str] = set()
    bus_names = _run_text(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.ListNames",
        ]
    )
    for name in re.findall(r"org\.mpris\.MediaPlayer2\.[A-Za-z0-9_.-]+", bus_names):
        status = _run_text(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                name,
                "--object-path",
                "/org/mpris/MediaPlayer2",
                "--method",
                "org.freedesktop.DBus.Properties.Get",
                "org.mpris.MediaPlayer2.Player",
                "PlaybackStatus",
            ],
            timeout=2.0,
        )
        if "Playing" in status:
            tokens.update(re.split(r"[^a-z0-9]+", name.lower()))

    try:
        dump = json.loads(_run_text(["pw-dump"], timeout=5.0) or "[]")
    except json.JSONDecodeError:
        dump = []
    if isinstance(dump, list):
        for item in dump:
            info = item.get("info", {}) if isinstance(item, Mapping) else {}
            props = info.get("props", {}) if isinstance(info, Mapping) else {}
            if not isinstance(props, Mapping):
                continue
            media_class = str(props.get("media.class", ""))
            if media_class.startswith("Stream/Output/Audio") and info.get("state") == "running":
                text = " ".join(
                    str(props.get(key, ""))
                    for key in ("application.name", "application.process.binary", "node.name", "media.name")
                ).lower()
                tokens.update(part for part in re.split(r"[^a-z0-9]+", text) if part)
    return tokens


def _pid_cgroup_unit(pid: int) -> str | None:
    try:
        for line in Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
            if line.startswith("0::"):
                component = line.rsplit("/", 1)[-1]
                return component if component.endswith(".scope") else None
    except OSError:
        return None
    return None


def active_inhibitor_units() -> set[str]:
    output = _run_text(["systemd-inhibit", "--list", "--no-pager"], timeout=4.0)
    lines = output.splitlines()
    if len(lines) < 2:
        return set()
    header = lines[0]
    pid_start = header.find("PID")
    comm_start = header.find("COMM")
    if pid_start < 0 or comm_start <= pid_start:
        return set()
    units: set[str] = set()
    for line in lines[1:]:
        field = line[pid_start:comm_start].strip()
        if field.isdigit():
            unit = _pid_cgroup_unit(int(field))
            if unit:
                units.add(unit)
    return units


def _matches_playback(app: AppCgroup, tokens: set[str]) -> bool:
    if not tokens:
        return False
    text = _scope_match_text(app.unit)
    candidate_tokens = set(part for part in re.split(r"[^a-z0-9]+", text) if len(part) >= 3)
    if app.app_id:
        candidate_tokens.add(app.app_id)
    return bool(candidate_tokens.intersection(tokens))


def load_activity(path: Path = ACTIVITY_PATH) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("version", 1)
    raw.setdefault("samples", {})
    raw.setdefault("cooldowns", {})
    raw.setdefault("reclaims", {})
    return raw


def save_activity(data: Mapping[str, Any], path: Path = ACTIVITY_PATH) -> None:
    atomic_write_json(path, data)


def _sample_activity(
    app: AppCgroup,
    previous: Mapping[str, Any] | None,
    now: float,
    settings: Mapping[str, Any],
) -> tuple[bool, float, int, float]:
    if not previous:
        return False, 0.0, 0, now
    elapsed = max(0.001, now - float(previous.get("timestamp", now)))
    cpu_delta = max(0, app.reading.cpu_usec - int(previous.get("cpu_usec", app.reading.cpu_usec)))
    io_delta = max(0, app.reading.io_bytes - int(previous.get("io_bytes", app.reading.io_bytes)))
    cpu_percent = cpu_delta / (elapsed * 1_000_000.0) * 100.0
    low = (
        cpu_percent < float(settings["memory"]["cpu_percent_max"])
        and io_delta < float(settings["memory"]["io_mib_per_sample_max"]) * MIB
    )
    idle_since = float(previous.get("idle_since", now)) if low else now
    return low, cpu_percent, io_delta, idle_since


def _recent_restore_status(app: AppCgroup, activity: dict[str, Any], now: float, settings: Mapping[str, Any]) -> bool:
    record = activity.get("reclaims", {}).get(app.key)
    if not isinstance(record, Mapping):
        return False
    reclaimed = max(0, int(record.get("actual_reduction", 0)))
    if reclaimed <= 0:
        return False
    elapsed = now - float(record.get("timestamp", 0.0))
    if elapsed < 0 or elapsed > 600:
        return False
    after = int(record.get("after_memory", app.reading.memory_current))
    returned = max(0, app.reading.memory_current - after)
    if returned < reclaimed * 0.70:
        return False
    activity.setdefault("cooldowns", {})[app.key] = now + float(settings["memory"]["refault_cooldown_minutes"]) * 60.0
    record = dict(record)
    record["rapid_refault"] = True
    activity["reclaims"][app.key] = record
    return True


def assess_cgroups(
    settings: Mapping[str, Any],
    *,
    automatic: bool,
    short_sample_seconds: float = 1.0,
    cgroup_root: Path | None = None,
    activity_path: Path = ACTIVITY_PATH,
) -> tuple[list[AppCgroup], list[AppDecision], dict[str, Any]]:
    activity = load_activity(activity_path)
    apps = discover_app_cgroups(settings, cgroup_root=cgroup_root)
    manual_baseline: dict[str, dict[str, Any]] = {}
    if not automatic and apps and short_sample_seconds > 0:
        baseline_time = time.time()
        manual_baseline = {
            item.key: {
                "timestamp": baseline_time,
                "cpu_usec": item.reading.cpu_usec,
                "io_bytes": item.reading.io_bytes,
                "memory_current": item.reading.memory_current,
                "idle_since": baseline_time,
            }
            for item in apps
        }
        time.sleep(min(5.0, max(0.0, short_sample_seconds)))
        refreshed = {item.unit: item for item in discover_app_cgroups(settings, cgroup_root=cgroup_root)}
        apps = [refreshed.get(item.unit, item) for item in apps]
    now = time.time()
    playback = active_playback_tokens()
    inhibitors = active_inhibitor_units()
    decisions: list[AppDecision] = []
    samples = activity.setdefault("samples", {})
    cooldowns = activity.setdefault("cooldowns", {})
    minimum = int(settings["memory"]["minimum_candidate_mib"]) * MIB

    for app in apps:
        previous = manual_baseline.get(app.key) if not automatic else (samples.get(app.key) if isinstance(samples, Mapping) else None)
        low, cpu_percent, io_delta, idle_since = _sample_activity(app, previous, now, settings)
        samples[app.key] = {
            "app_id": app.app_id,
            "timestamp": now,
            "cpu_usec": app.reading.cpu_usec,
            "io_bytes": app.reading.io_bytes,
            "memory_current": app.reading.memory_current,
            "idle_since": idle_since,
        }
        recently_restored = _recent_restore_status(app, activity, now, settings)
        cooldown_until = float(cooldowns.get(app.key, 0.0))
        playing = _matches_playback(app, playback)
        inhibited = app.unit in inhibitors
        if playing or inhibited:
            idle_since = now
            samples[app.key]["idle_since"] = now
        status = "Busy"
        eligible = True
        reason = "Eligible for transparent page reclaim"

        if app.protected_reason or app.policy == POLICY_NEVER or app.app_id is None:
            status, eligible, reason = "Protected", False, app.protected_reason or "Policy is Never park"
        elif automatic and app.policy != POLICY_AUTO:
            status, eligible, reason = "Protected", False, "Policy allows manual parking only"
        elif not automatic and app.policy not in (POLICY_AUTO, POLICY_MANUAL):
            status, eligible, reason = "Protected", False, "Policy is Never park"
        elif playing:
            status, eligible, reason = "Playing", False, "Active media playback"
        elif inhibited:
            status, eligible, reason = "Busy", False, "Active session inhibitor"
        elif recently_restored:
            status, eligible, reason = "Recently restored", False, "Memory faulted back rapidly; cooldown extended"
        elif cooldown_until > now:
            status, eligible, reason = "Recently restored", False, f"Cooldown has {math.ceil((cooldown_until - now) / 60)} min remaining"
        elif app.reading.memory_current < minimum:
            status, eligible, reason = "Idle" if low else "Busy", False, f"Below {format_bytes(minimum)} minimum"
        elif automatic:
            idle_seconds = now - idle_since
            required = int(settings["memory"]["idle_minutes"]) * 60
            if previous is None:
                status, eligible, reason = "Busy", False, "Collecting the first idle activity sample"
            elif not low:
                status, eligible, reason = "Busy", False, f"CPU {cpu_percent:.2f}% or I/O {format_bytes(io_delta)} exceeded idle limit"
            elif idle_seconds < required:
                status, eligible, reason = "Idle", False, f"Idle history {int(idle_seconds // 60)}/{required // 60} min"
            else:
                status = "Idle"
        else:
            if previous and not low:
                status, eligible, reason = "Busy", False, f"Recent CPU {cpu_percent:.2f}% or I/O {format_bytes(io_delta)}"
            else:
                status = "Idle"

        decisions.append(
            AppDecision(
                unit=app.unit,
                app_id=app.app_id,
                label=app.label,
                policy=app.policy,
                status=status,
                memory_current=app.reading.memory_current,
                eligible=eligible,
                reason=reason,
            )
        )
        samples[app.key]["status"] = status
        samples[app.key]["reason"] = reason

    active_keys = {app.key for app in apps}
    activity["samples"] = {key: value for key, value in samples.items() if key in active_keys}
    activity["cooldowns"] = {key: value for key, value in cooldowns.items() if float(value) > now - 3600}
    activity["reclaims"] = {
        key: value for key, value in activity.get("reclaims", {}).items()
        if isinstance(value, Mapping) and float(value.get("timestamp", 0.0)) > now - 86400
    }
    save_activity(activity, activity_path)
    return apps, decisions, activity


def _safe_reclaim_file(app: AppCgroup, cgroup_root: Path) -> Path:
    if app.path.name != app.unit or not app.unit.endswith(".scope"):
        raise PermissionError("Only application scope cgroups are eligible")
    resolved_root = cgroup_root.resolve(strict=True)
    resolved_app = app.path.resolve(strict=True)
    if resolved_app.parent != resolved_root:
        raise PermissionError("Cgroup left the fixed graphical app.slice root")
    if resolved_app.stat(follow_symlinks=False).st_uid != os.getuid():
        raise PermissionError("Cgroup is not owned by the current user")
    reclaim = resolved_app / "memory.reclaim"
    if reclaim.is_symlink() or not reclaim.exists() or not os.access(reclaim, os.W_OK):
        raise PermissionError("Writable cgroup memory.reclaim is unavailable")
    return reclaim


def write_memory_reclaim(app: AppCgroup, amount_bytes: int, cgroup_root: Path | None = None) -> None:
    root = default_cgroup_root() if cgroup_root is None else cgroup_root
    reclaim = _safe_reclaim_file(app, root)
    flags = os.O_WRONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(reclaim, flags)
    try:
        os.write(descriptor, f"{int(amount_bytes)} swappiness=max\n".encode("ascii"))
    finally:
        os.close(descriptor)


def reclaim_supported(cgroup_root: Path | None = None) -> tuple[bool, str]:
    root = default_cgroup_root() if cgroup_root is None else cgroup_root
    try:
        resolved = root.resolve(strict=True)
    except OSError:
        return False, "The graphical app.slice cgroup is unavailable"
    if resolved.stat(follow_symlinks=False).st_uid != os.getuid():
        return False, "The graphical app.slice is not user-owned"
    candidates = [item for item in resolved.iterdir() if item.name.endswith(".scope") and item.is_dir()]
    if not candidates:
        return False, "No user application scopes are currently available"
    if not any((item / "memory.reclaim").exists() and os.access(item / "memory.reclaim", os.W_OK) for item in candidates):
        return False, "User-writable cgroup memory.reclaim is unavailable"
    return True, "Available"


def _reclaim_request_bytes(current: int, settings: Mapping[str, Any]) -> int:
    memory = settings["memory"]
    requested = int(current * float(memory["reclaim_percent"]) / 100.0)
    minimum = int(memory["minimum_reclaim_mib"]) * MIB
    maximum = int(memory["maximum_reclaim_mib"]) * MIB
    return min(maximum, max(minimum, requested))


def _swap_reclaim_budget(snapshot: MemorySnapshot, settings: Mapping[str, Any]) -> int:
    memory = settings["memory"]
    reserve = max(
        int(float(memory["swap_reserve_gib"]) * GIB),
        int(snapshot.swap_total_bytes * float(memory["swap_reserve_percent"]) / 100.0),
    )
    return max(0, snapshot.swap_free_bytes - reserve)


def run_memory_relief(
    settings: Mapping[str, Any],
    *,
    automatic: bool,
    dry_run: bool = False,
    cgroup_root: Path | None = None,
    activity_path: Path = ACTIVITY_PATH,
) -> MemoryRunResult:
    before = read_memory_snapshot()
    result = MemoryRunResult(
        mode=str(settings.get("memory_mode", "off")),
        automatic=automatic,
        dry_run=dry_run,
        pressure_triggered=pressure_triggered(before, settings),
        before=before,
        after=before,
    )
    if not settings.get("setup_complete"):
        result.error = "Initial setup is not complete; automation is disabled"
        return result
    if settings.get("memory_mode") == "off":
        result.stopped_reason = "Memory care is turned off"
        return result
    if automatic and settings.get("memory_mode") != "pressure-aware":
        result.stopped_reason = "Automatic memory care is not enabled"
        return result
    if not result.pressure_triggered:
        result.stopped_reason = "Memory pressure is comfortable; nothing was needed"
        return result
    if before.swap_total_bytes <= 0:
        result.error = "Swap is unavailable; RAM Relief will not park memory"
        return result
    supported, support_reason = reclaim_supported(cgroup_root)
    if not supported:
        result.error = support_reason
        return result

    apps, decisions, activity = assess_cgroups(
        settings,
        automatic=automatic,
        cgroup_root=cgroup_root,
        activity_path=activity_path,
    )
    result.decisions = decisions
    app_by_unit = {app.unit: app for app in apps}
    eligible = [decision for decision in decisions if decision.eligible]
    eligible.sort(key=lambda item: item.memory_current, reverse=True)

    memory = settings["memory"]
    run_cap = int(before.total_bytes * float(memory["run_cap_percent_ram"]) / 100.0)
    stop_target = int(before.total_bytes * float(memory["available_stop_percent"]) / 100.0)
    needed = max(0, stop_target - before.available_bytes)
    remaining = min(run_cap, needed, _swap_reclaim_budget(before, settings))
    if remaining <= 0:
        result.stopped_reason = "Swap reserve or available-memory target leaves no safe reclaim budget"
        return result

    root = default_cgroup_root() if cgroup_root is None else cgroup_root
    now = time.time()
    for decision in eligible:
        if remaining <= 0:
            result.stopped_reason = "Safe per-run reclaim budget reached"
            break
        app = app_by_unit.get(decision.unit)
        if app is None:
            continue
        try:
            current_before = read_cgroup(app.path)
        except (OSError, ValueError):
            continue
        requested = min(_reclaim_request_bytes(current_before.memory_current, settings), remaining)
        if requested <= 0:
            continue
        if dry_run:
            result.parked.append(ParkedApp(app.app_id or "unknown", app.label, requested, 0, 0))
            remaining -= requested
            continue
        partial_error = False
        try:
            write_memory_reclaim(app, requested, root)
        except OSError as error:
            if error.errno == errno.EAGAIN:
                partial_error = True
            else:
                if result.error is None:
                    result.error = f"{app.label}: reclaim was unavailable ({error})"
                continue
        except PermissionError as error:
            if result.error is None:
                result.error = f"{app.label}: reclaim was unavailable ({error})"
            continue
        time.sleep(0.15)
        try:
            current_after = read_cgroup(app.path)
        except (OSError, ValueError):
            current_after = current_before
        actual = max(0, current_before.memory_current - current_after.memory_current)
        swap_growth = max(0, current_after.swap_current - current_before.swap_current)
        result.parked.append(ParkedApp(app.app_id or "unknown", app.label, requested, actual, swap_growth))
        if partial_error and result.stopped_reason is None:
            result.stopped_reason = f"{app.label} reclaimed less than requested; it was not retried"
        remaining -= requested
        cooldown = now + float(memory["cooldown_minutes"]) * 60.0
        activity.setdefault("cooldowns", {})[app.key] = cooldown
        activity.setdefault("reclaims", {})[app.key] = {
            "app_id": app.app_id,
            "timestamp": now,
            "before_memory": current_before.memory_current,
            "after_memory": current_after.memory_current,
            "actual_reduction": actual,
            "rapid_refault": False,
        }
        current_system = read_memory_snapshot()
        result.after = current_system
        if current_system.available_bytes >= stop_target:
            result.stopped_reason = "Available RAM reached the comfort target"
            break
        if _swap_reclaim_budget(current_system, settings) <= 0:
            result.stopped_reason = "Swap reserve reached"
            break

    if not dry_run:
        save_activity(activity, activity_path)
        result.after = read_memory_snapshot()
    if not result.parked and result.stopped_reason is None and result.error is None:
        result.stopped_reason = "No approved application was safely eligible"
    return result


def append_aggregate_history(entry: Mapping[str, Any], path: Path = HISTORY_PATH, max_entries: int = 60) -> None:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    safe_entry = {"timestamp": int(time.time()), **dict(entry)}
    existing.append(safe_entry)
    atomic_write_json(path, existing[-max_entries:])


def timer_should_be_enabled(settings: Mapping[str, Any]) -> bool:
    return bool(settings.get("setup_complete") and settings.get("memory_mode") == "pressure-aware")


def status_document(settings: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = read_memory_snapshot()
    supported, support_reason = reclaim_supported()
    return {
        "name": APP_NAME,
        "subtitle": APP_SUBTITLE,
        "schema_version": settings.get("schema_version"),
        "setup_complete": bool(settings.get("setup_complete")),
        "memory_mode": settings.get("memory_mode"),
        "pressure": {
            "triggered": pressure_triggered(snapshot, settings),
            "available_bytes": snapshot.available_bytes,
            "available_percent": snapshot.available_percent,
            "psi_some_avg10": snapshot.psi_some_avg10,
            "swap_free_bytes": snapshot.swap_free_bytes,
        },
        "memory_reclaim": {"available": supported, "reason": support_reason},
        "cleanup_enabled": [
            key for key, enabled in settings["cleanup"]["categories"].items() if enabled
        ],
    }


__all__ = [
    "ACTIVITY_PATH",
    "APP_BY_ID",
    "APP_CATALOG",
    "APP_ID",
    "APP_NAME",
    "APP_SUBTITLE",
    "CONFIG_DIR",
    "DEFAULT_SETTINGS",
    "GIB",
    "HISTORY_PATH",
    "MIB",
    "POLICY_AUTO",
    "POLICY_LABELS",
    "POLICY_MANUAL",
    "POLICY_NEVER",
    "SCHEMA_VERSION",
    "SETTINGS_PATH",
    "STATE_DIR",
    "AppCgroup",
    "AppDecision",
    "AppSpec",
    "CgroupReading",
    "MemoryRunResult",
    "MemorySnapshot",
    "SettingsStore",
    "append_aggregate_history",
    "assess_cgroups",
    "atomic_write_json",
    "classify_scope",
    "decode_systemd_unit",
    "default_cgroup_root",
    "discover_app_cgroups",
    "format_bytes",
    "load_activity",
    "normalize_settings",
    "pressure_triggered",
    "read_cgroup",
    "read_memory_snapshot",
    "reclaim_supported",
    "run_memory_relief",
    "status_document",
    "timer_should_be_enabled",
    "write_memory_reclaim",
]
