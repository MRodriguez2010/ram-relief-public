#!/usr/bin/env python3
"""Lightweight native GTK interface for RAM Relief v2."""

from __future__ import annotations

import copy
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from ram_relief_cleanup import CleanupResult, CleanupScan, run_cleanup, scan_cleanup
from ram_relief_core import (
    APP_CATALOG,
    APP_ID,
    APP_NAME,
    APP_SUBTITLE,
    POLICY_AUTO,
    POLICY_LABELS,
    POLICY_MANUAL,
    POLICY_NEVER,
    MemoryRunResult,
    SettingsStore,
    append_aggregate_history,
    discover_app_cgroups,
    format_bytes,
    load_activity,
    pressure_triggered,
    read_memory_snapshot,
    reclaim_supported,
    timer_should_be_enabled,
)


SCRIPT_DIR = Path(__file__).resolve().parent
STYLE_PATH = SCRIPT_DIR / "style.css"
ACTION_ASSET_DIR = SCRIPT_DIR / "assets" / "actions"

MODE_VALUES = ("pressure-aware", "manual-only", "off")
MODE_LABELS = ("Pressure-aware automatic", "Manual Optimize only", "Memory care off")
THEME_VALUES = ("system", "dark", "light")
THEME_LABELS = ("Follow system", "Dark", "Light")
MOTION_VALUES = ("system", "reduce", "full")
MOTION_LABELS = ("Follow system", "Reduce motion", "Full motion")
POLICY_VALUES = (POLICY_AUTO, POLICY_MANUAL, POLICY_NEVER)
POLICY_OPTIONS = tuple(POLICY_LABELS[value] for value in POLICY_VALUES)

CATEGORY_COPY: dict[str, tuple[str, str]] = {
    "thumbnails": ("Thumbnail cache", "Generated previews older than the stale threshold"),
    "browser_cache": ("Browser web cache", "Only stale cache files, and only while that browser is fully stopped"),
    "trash": ("Local home Trash", "Freedesktop Trash API with original-path exclusions"),
    "npm": ("npm download cache", "Optional; packages download again when needed"),
    "yarn": ("Yarn download cache", "Optional; packages download again when needed"),
    "pip": ("pip download cache", "Optional; packages download again when needed"),
    "playwright": ("Playwright browser bundles", "Off by default; automation must redownload deleted browsers"),
}

APP_ICONS = {
    "zen": "web-browser-symbolic",
    "firefox": "web-browser-symbolic",
    "brave": "web-browser-symbolic",
    "discord": "chat-symbolic",
    "slack": "chat-symbolic",
    "teams": "system-users-symbolic",
    "telegram": "mail-send-symbolic",
    "obsidian": "accessories-text-editor-symbolic",
    "spotify": "audio-x-generic-symbolic",
    "steam": "applications-games-symbolic",
    "thunderbird": "mail-unread-symbolic",
    "vlc": "video-x-generic-symbolic",
    "vscode": "text-x-script-symbolic",
    "chrome": "web-browser-symbolic",
    "chromium": "web-browser-symbolic",
    "codex": "applications-development-symbolic",
}


def css(widget: Gtk.Widget, *names: str) -> Gtk.Widget:
    context = widget.get_style_context()
    for name in names:
        context.add_class(name)
    return widget


def make_label(
    text: str = "",
    *,
    style: str | None = None,
    wrap: bool = False,
    xalign: float = 0.0,
) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=xalign)
    if wrap:
        label.set_line_wrap(True)
        label.set_line_wrap_mode(2)
    if style:
        css(label, style)
    return label


def make_icon(name: str, size: Gtk.IconSize = Gtk.IconSize.BUTTON) -> Gtk.Image:
    return Gtk.Image.new_from_icon_name(name, size)


def _run_systemctl(arguments: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["systemctl", "--user", *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    detail = (completed.stderr or completed.stdout).strip()
    return completed.returncode == 0, detail


def sync_timer(settings: Mapping[str, Any]) -> tuple[bool, str]:
    _run_systemctl(["daemon-reload"])
    if timer_should_be_enabled(settings):
        return _run_systemctl(["enable", "--now", "ram-relief.timer"])
    success, detail = _run_systemctl(["disable", "--now", "ram-relief.timer"])
    if not success and "does not exist" in detail.lower():
        return True, ""
    return success, detail


def timer_state() -> str:
    enabled, _detail = _run_systemctl(["is-enabled", "ram-relief.timer"])
    active, _active_detail = _run_systemctl(["is-active", "ram-relief.timer"])
    if enabled and active:
        return "Enabled • checks every 2 minutes"
    if enabled:
        return "Enabled • waiting for user session"
    return "Off"


class FrameAnimator:
    """Lazy 12-frame loader; decoded frames are released after each action."""

    def __init__(self, image: Gtk.Image, reduced_motion: Callable[[], bool]) -> None:
        self.image = image
        self.reduced_motion = reduced_motion
        self.frames: list[GdkPixbuf.Pixbuf] = []
        self.index = 0
        self.source_id = 0

    def start(self, action: str) -> None:
        self.stop(show_static=False)
        paths = sorted((ACTION_ASSET_DIR / action).glob("frame-*.webp"))[:12]
        if not paths:
            return
        try:
            chosen = paths[:1] if self.reduced_motion() else paths
            self.frames = [
                GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 320, 320, True)
                for path in chosen
            ]
        except GLib.Error:
            self.frames.clear()
            return
        self.image.set_from_pixbuf(self.frames[0])
        self.image.show()
        if len(self.frames) > 1:
            self.source_id = GLib.timeout_add(83, self._advance)

    def _advance(self) -> bool:
        if not self.frames:
            return False
        self.index = (self.index + 1) % len(self.frames)
        self.image.set_from_pixbuf(self.frames[self.index])
        return True

    def stop(self, *, show_static: bool = True) -> None:
        if self.source_id:
            GLib.source_remove(self.source_id)
            self.source_id = 0
        self.frames.clear()
        self.index = 0
        self.image.clear()
        self.image.hide()
        static = ACTION_ASSET_DIR / "overview" / "static.webp"
        if show_static and static.exists():
            try:
                self.image.set_from_pixbuf(
                    GdkPixbuf.Pixbuf.new_from_file_at_scale(str(static), 320, 320, True)
                )
                self.image.show()
            except GLib.Error:
                self.image.hide()


class RamReliefWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application, *, demo: bool = False) -> None:
        super().__init__(application=application)
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.store.save()
        self.demo = demo
        qa_theme = os.environ.get("RAM_RELIEF_QA_THEME") if demo else None
        if qa_theme in THEME_VALUES:
            self.settings["theme"] = qa_theme
        self.busy = False
        self.cleanup_scan: CleanupScan | None = None
        self.nav_buttons: dict[str, Gtk.RadioButton] = {}
        self.policy_subtitles: dict[str, Gtk.Label] = {}
        self.category_switches: dict[str, list[Gtk.Switch]] = {}
        self.exclusion_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.result_values: dict[str, Gtk.Label] = {}
        self.notice_timeout = 0
        self._system_dark = self._detect_system_dark()
        self.interface_settings: Gio.Settings | None = None
        try:
            self.interface_settings = Gio.Settings.new("org.gnome.desktop.interface")
            self.interface_settings.connect("changed::color-scheme", self._on_system_theme_changed)
        except GLib.Error:
            self.interface_settings = None

        self.set_title(f"{APP_NAME} — {APP_SUBTITLE}")
        self.set_default_size(1120, 760)
        self.set_size_request(860, 640)
        self.set_icon_name("ram-relief")
        self._build_header()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        body.set_hexpand(True)
        body.set_vexpand(True)
        outer.pack_start(body, True, True, 0)
        self.rail = self._build_rail()
        body.pack_start(self.rail, False, False, 0)
        body.pack_start(css(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), "rail-separator"), False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(160)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        body.pack_start(self.stack, True, True, 0)

        self.stack.add_named(self._build_overview(), "overview")
        self.stack.add_named(self._build_memory_page(), "memory")
        self.stack.add_named(self._build_cleanup_page(), "cleanup")
        self.stack.add_named(self._build_settings_page(), "settings")
        self.stack.add_named(self._build_setup_page(), "setup")

        self.notice_revealer = Gtk.Revealer()
        self.notice_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.notice_revealer.set_transition_duration(140)
        self.notice_label = make_label(style="notice-label", wrap=True, xalign=0.5)
        self.notice_revealer.add(self.notice_label)
        outer.pack_end(self.notice_revealer, False, False, 0)

        self._apply_theme()
        self._set_navigation_enabled(bool(self.settings["setup_complete"]) or self.demo)
        self.show_page("overview" if self.settings["setup_complete"] or self.demo else "setup", force=True)
        self.show_all()
        self.animator.stop(show_static=True)
        self.refresh_live_state()
        if self.settings["setup_complete"] or self.demo:
            self._start_cleanup_scan()
        GLib.timeout_add_seconds(30, self._periodic_refresh)

    def _build_header(self) -> None:
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.pack_start(make_label(APP_NAME, style="header-title", xalign=0.5), False, False, 0)
        title_box.pack_start(make_label(APP_SUBTITLE, style="header-subtitle", xalign=0.5), False, False, 0)
        title_box.show_all()
        header.set_custom_title(title_box)
        css(header, "app-header")
        self.header = header
        refresh = Gtk.Button(image=make_icon("view-refresh-symbolic"))
        refresh.set_relief(Gtk.ReliefStyle.NONE)
        refresh.set_tooltip_text("Refresh current memory and eligibility")
        refresh.connect("clicked", lambda _button: self.refresh_live_state(show_notice=True))
        header.pack_end(refresh)
        self.set_titlebar(header)

    def _build_rail(self) -> Gtk.Widget:
        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        rail.set_size_request(210, -1)
        css(rail, "navigation-rail")
        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        css(brand, "rail-brand")
        brand.pack_start(make_icon("speedometer-symbolic", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        copy_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        copy_box.pack_start(make_label("RAM Relief", style="rail-brand-title"), False, False, 0)
        copy_box.pack_start(make_label("Care, not force", style="rail-brand-caption"), False, False, 0)
        brand.pack_start(copy_box, True, True, 0)
        rail.pack_start(brand, False, False, 0)

        group: Gtk.RadioButton | None = None
        for name, label, icon in (
            ("overview", "Overview", "view-grid-symbolic"),
            ("memory", "Memory", "speedometer-symbolic"),
            ("cleanup", "Cleanup", "drive-harddisk-symbolic"),
            ("settings", "Settings", "preferences-system-symbolic"),
        ):
            button = Gtk.RadioButton.new_from_widget(group) if group else Gtk.RadioButton.new(None)
            if group is None:
                group = button
            button.set_mode(False)
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_hexpand(True)
            css(button, "nav-button")
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            content.pack_start(make_icon(icon), False, False, 0)
            content.pack_start(make_label(label, style="nav-label"), True, True, 0)
            button.add(content)
            button.connect("toggled", self._on_nav_toggled, name)
            rail.pack_start(button, False, False, 0)
            self.nav_buttons[name] = button

        spacer = Gtk.Box()
        rail.pack_start(spacer, True, True, 0)
        safety = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        css(safety, "rail-safety")
        safety.pack_start(make_icon("security-high-symbolic"), False, False, 0)
        safety.pack_start(make_label("No app closing\nNo root access", style="rail-safety-copy", xalign=0.5), False, False, 0)
        rail.pack_end(safety, False, False, 0)
        return rail

    def _on_nav_toggled(self, button: Gtk.RadioButton, name: str) -> None:
        if button.get_active() and (self.settings["setup_complete"] or self.demo):
            self.stack.set_visible_child_name(name)

    def _set_navigation_enabled(self, enabled: bool) -> None:
        rail_context = self.rail.get_style_context()
        if enabled:
            rail_context.remove_class("locked")
        else:
            rail_context.add_class("locked")
        for button in self.nav_buttons.values():
            button.set_sensitive(enabled)

    def show_page(self, name: str, *, force: bool = False) -> None:
        if not force and not self.settings["setup_complete"] and not self.demo:
            name = "setup"
        self.stack.set_visible_child_name(name)
        if name in self.nav_buttons:
            self.nav_buttons[name].set_active(True)

    def _scrolled_page(self, content: Gtk.Widget, width: int = 900) -> Gtk.Widget:
        content.set_size_request(width, -1)
        content.set_halign(Gtk.Align.CENTER)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(content)
        return scroller

    def _build_overview(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        css(page, "page")
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
        css(hero, "hero-card")
        copy_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        copy_box.set_hexpand(True)
        copy_box.set_valign(Gtk.Align.CENTER)
        copy_box.pack_start(make_label("PRESSURE-AWARE CARE", style="eyebrow"), False, False, 0)
        copy_box.pack_start(make_label("Make room without closing your work", style="display-title", wrap=True), False, False, 0)
        self.hero_detail = make_label(
            "Linux can move idle app pages into swap and restore them automatically when you return.",
            style="body-muted",
            wrap=True,
        )
        copy_box.pack_start(self.hero_detail, False, False, 0)
        self.hero_memory = make_label("—", style="hero-number")
        copy_box.pack_start(self.hero_memory, False, False, 0)
        self.hero_memory_caption = make_label("available now", style="caption")
        copy_box.pack_start(self.hero_memory_caption, False, False, 0)
        hero.pack_start(copy_box, True, True, 0)
        self.action_image = Gtk.Image()
        self.action_image.set_size_request(220, 220)
        self.action_image.set_no_show_all(True)
        hero.pack_end(self.action_image, False, False, 0)
        self.animator = FrameAnimator(self.action_image, self._motion_reduced)
        page.pack_start(hero, False, False, 0)

        metrics = Gtk.Grid(column_spacing=12, row_spacing=12)
        metrics.set_column_homogeneous(True)
        self.potential_metric = self._metric_card("Potential memory relief", "—", "speedometer-symbolic")
        self.cleanup_metric = self._metric_card("Safe cleanup", "Scanning…", "drive-harddisk-symbolic")
        self.protected_metric = self._metric_card("Protected scopes", "—", "security-high-symbolic")
        metrics.attach(self.potential_metric[0], 0, 0, 1, 1)
        metrics.attach(self.cleanup_metric[0], 1, 0, 1, 1)
        metrics.attach(self.protected_metric[0], 2, 0, 1, 1)
        page.pack_start(metrics, False, False, 0)

        action_card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        css(action_card, "action-card")
        action_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        action_copy.set_hexpand(True)
        action_copy.pack_start(make_label("One calm pass", style="section-title"), False, False, 0)
        action_copy.pack_start(
            make_label(
                "Memory is parked only under real pressure. Your approved stale cleanup runs in the same click.",
                style="body-muted",
                wrap=True,
            ),
            False,
            False,
            0,
        )
        action_card.pack_start(action_copy, True, True, 0)
        self.optimize_button = Gtk.Button()
        css(self.optimize_button, "optimize-button")
        self.optimize_button.set_valign(Gtk.Align.CENTER)
        self.optimize_button.connect("clicked", self._on_optimize)
        action_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        self.optimize_spinner = Gtk.Spinner()
        self.optimize_spinner.set_no_show_all(True)
        self.optimize_icon = make_icon("media-playback-start-symbolic")
        self.optimize_label = make_label("Optimize", style="action-label")
        action_content.pack_start(self.optimize_spinner, False, False, 0)
        action_content.pack_start(self.optimize_icon, False, False, 0)
        action_content.pack_start(self.optimize_label, False, False, 0)
        self.optimize_button.add(action_content)
        action_card.pack_end(self.optimize_button, False, False, 0)
        page.pack_start(action_card, False, False, 0)

        self.result_revealer = Gtk.Revealer()
        self.result_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.result_revealer.set_transition_duration(180)
        result = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        css(result, "result-card")
        result_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.result_icon = make_icon("emblem-ok-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        result_header.pack_start(self.result_icon, False, False, 0)
        result_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.result_title = make_label("Ready", style="result-title")
        self.result_detail = make_label(style="body-muted", wrap=True)
        result_copy.pack_start(self.result_title, False, False, 0)
        result_copy.pack_start(self.result_detail, False, False, 0)
        result_header.pack_start(result_copy, True, True, 0)
        result.pack_start(result_header, False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=5)
        grid.set_column_homogeneous(True)
        for column, (key, label) in enumerate(
            (
                ("memory", "Measured app-memory reduction"),
                ("available", "Available-memory change"),
                ("swap", "Swap change"),
                ("cleanup", "Storage cleared"),
            )
        ):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            value = make_label("—", style="result-value")
            box.pack_start(value, False, False, 0)
            box.pack_start(make_label(label, style="caption", wrap=True), False, False, 0)
            grid.attach(box, column, 0, 1, 1)
            self.result_values[key] = value
        result.pack_start(grid, False, False, 0)
        self.result_revealer.add(result)
        page.pack_start(self.result_revealer, False, False, 0)
        return self._scrolled_page(page)

    def _metric_card(self, title: str, value: str, icon: str) -> tuple[Gtk.Widget, Gtk.Label]:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        css(card, "metric-card")
        image = make_icon(icon)
        image.set_halign(Gtk.Align.START)
        css(image, "metric-icon")
        card.pack_start(image, False, False, 0)
        value_label = make_label(value, style="metric-value")
        card.pack_start(value_label, False, False, 0)
        card.pack_start(make_label(title, style="caption", wrap=True), False, False, 0)
        return card, value_label

    def _section(self, title: str, description: str = "") -> tuple[Gtk.Box, Gtk.Box]:
        section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        css(section, "preference-section")
        section.pack_start(make_label(title, style="section-title"), False, False, 0)
        if description:
            section.pack_start(make_label(description, style="body-muted", wrap=True), False, False, 0)
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        css(rows, "preference-list")
        section.pack_start(rows, False, False, 0)
        return section, rows

    def _preference_row(
        self,
        title: str,
        subtitle: str,
        *,
        icon: str | None = None,
        suffix: Gtk.Widget | None = None,
    ) -> tuple[Gtk.Box, Gtk.Label]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        css(row, "preference-row")
        if icon:
            image = make_icon(icon)
            image.set_valign(Gtk.Align.CENTER)
            row.pack_start(image, False, False, 0)
        copy_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        copy_box.set_hexpand(True)
        copy_box.pack_start(make_label(title, style="preference-title"), False, False, 0)
        subtitle_label = make_label(subtitle, style="caption", wrap=True)
        copy_box.pack_start(subtitle_label, False, False, 0)
        row.pack_start(copy_box, True, True, 0)
        if suffix:
            suffix.set_valign(Gtk.Align.CENTER)
            row.pack_end(suffix, False, False, 0)
        return row, subtitle_label

    def _combo(self, labels: tuple[str, ...], selected: int) -> Gtk.ComboBoxText:
        combo = Gtk.ComboBoxText()
        for label in labels:
            combo.append_text(label)
        combo.set_active(selected)
        return combo

    def _preferences_page(self) -> Gtk.Box:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        css(page, "preferences-page", "page")
        return page

    def _build_memory_page(self) -> Gtk.Widget:
        page = self._preferences_page()
        mode_section, mode_rows = self._section(
            "Memory parking",
            "Anonymous pages may move to swap while the application stays alive and restores automatically on return.",
        )
        self.mode_combo = self._combo(MODE_LABELS, MODE_VALUES.index(self.settings["memory_mode"]))
        self.mode_combo.connect("changed", self._on_mode_changed)
        row, _subtitle = self._preference_row(
            "Mode", "Automatic checks run only under measured pressure", icon="speedometer-symbolic", suffix=self.mode_combo
        )
        mode_rows.pack_start(row, False, False, 0)
        timer_row, self.timer_subtitle = self._preference_row(
            "Automatic check", timer_state(), icon="alarm-symbolic"
        )
        mode_rows.pack_start(timer_row, False, False, 0)
        threshold_row, _ = self._preference_row(
            "Pressure gate",
            "Below 20% available RAM or PSI some avg10 at 2%; stops at 30% available",
            icon="dialog-information-symbolic",
        )
        mode_rows.pack_start(threshold_row, False, False, 0)
        page.pack_start(mode_section, False, False, 0)

        apps_section, apps_rows = self._section(
            "Application policies",
            "Unknown applications and essential tools stay protected. Running status comes from the latest safe activity sample.",
        )
        for spec in APP_CATALOG:
            if spec.protected_reason:
                suffix = make_icon("changes-prevent-symbolic")
            else:
                suffix = self._combo(POLICY_OPTIONS, POLICY_VALUES.index(self.settings["app_policies"][spec.app_id]))
                suffix.connect("changed", self._on_policy_changed, spec.app_id)
            row, subtitle = self._preference_row(
                spec.label,
                ("Protected" if spec.protected_reason else POLICY_LABELS[self.settings["app_policies"][spec.app_id]]) + " • not running",
                icon=APP_ICONS.get(spec.app_id, "application-x-executable-symbolic"),
                suffix=suffix,
            )
            apps_rows.pack_start(row, False, False, 0)
            self.policy_subtitles[spec.app_id] = subtitle
        page.pack_start(apps_section, False, False, 0)

        limits_section, limit_rows = self._section("Built-in limits", "Conservative limits stay visible so automatic runs are never mysterious.")
        for title, subtitle in (
            ("10-minute idle history", "Under 1% average CPU and under 4 MiB cgroup I/O per sample"),
            ("192 MiB minimum", "Smaller application scopes are left alone"),
            ("35% per application", "At least 64 MiB, at most 512 MiB; one attempt per run"),
            ("Swap reserve", "Keeps the greater of 2 GiB or 25% of total swap free"),
            ("Cooldown", "30 minutes, extended to 60 minutes after rapid refault"),
        ):
            row, _ = self._preference_row(title, subtitle)
            limit_rows.pack_start(row, False, False, 0)
        page.pack_start(limits_section, False, False, 0)
        return self._scrolled_page(page)

    def _build_cleanup_page(self) -> Gtk.Widget:
        page = self._preferences_page()
        summary_section, summary_rows = self._section(
            "Guarded storage care",
            "Cleanup runs only from Optimize. Every item is manifested and revalidated immediately before deletion.",
        )
        scan_button = Gtk.Button(label="Detailed scan")
        scan_button.connect("clicked", lambda _button: self._start_cleanup_scan(show_notice=True))
        summary_row, self.cleanup_summary_subtitle = self._preference_row(
            "Approved cleanup scan", "Not scanned yet", icon="drive-harddisk-symbolic", suffix=scan_button
        )
        summary_rows.pack_start(summary_row, False, False, 0)
        page.pack_start(summary_section, False, False, 0)

        categories_section, category_rows = self._section(
            "Cleanup categories", "Enabled categories run without another prompt after setup."
        )
        for category, (title, subtitle) in CATEGORY_COPY.items():
            toggle = Gtk.Switch()
            toggle.set_active(bool(self.settings["cleanup"]["categories"][category]))
            toggle.connect("notify::active", self._on_cleanup_category_changed, category)
            row, _ = self._preference_row(title, subtitle, suffix=toggle)
            category_rows.pack_start(row, False, False, 0)
            self.category_switches.setdefault(category, []).append(toggle)
        page.pack_start(categories_section, False, False, 0)

        age_section, age_rows = self._section("Stale age")
        adjustment = Gtk.Adjustment(value=int(self.settings["cleanup"]["stale_days"]), lower=1, upper=365, step_increment=1)
        self.age_spin = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
        self.age_spin.set_width_chars(4)
        self.age_spin.connect("value-changed", self._on_stale_age_changed)
        row, _ = self._preference_row(
            "Cache files must be older than",
            "Days; Trash is handled separately through its original location",
            suffix=self.age_spin,
        )
        age_rows.pack_start(row, False, False, 0)
        page.pack_start(age_section, False, False, 0)

        exclusions_section, exclusion_rows = self._section(
            "Exclusions", "Exclusions only reduce cleanup scope and also protect matching Trash origins."
        )
        exclusion_rows.pack_start(self.exclusion_rows, False, False, 0)
        self._rebuild_exclusion_rows()
        page.pack_start(exclusions_section, False, False, 0)

        protected_section, protected_rows = self._section("Always protected storage", "No cleaner path points at these locations.")
        row, _ = self._preference_row(
            "Profiles, credentials, configuration, app data, Codex, package state, system paths, and mounted storage",
            "Includes Codex runtimes/update caches, Chrome debug profiles, /tmp, /var, external drives, and NAS mounts",
            icon="security-high-symbolic",
        )
        protected_rows.pack_start(row, False, False, 0)
        page.pack_start(protected_section, False, False, 0)
        return self._scrolled_page(page)

    def _build_settings_page(self) -> Gtk.Widget:
        page = self._preferences_page()
        appearance, appearance_rows = self._section("Appearance")
        self.theme_combo = self._combo(THEME_LABELS, THEME_VALUES.index(self.settings["theme"]))
        self.theme_combo.connect("changed", self._on_theme_changed)
        row, _ = self._preference_row("Theme", "Graphite dark and cool off-white light", suffix=self.theme_combo)
        appearance_rows.pack_start(row, False, False, 0)
        self.motion_combo = self._combo(MOTION_LABELS, MOTION_VALUES.index(self.settings["reduced_motion"]))
        self.motion_combo.connect("changed", self._on_motion_changed)
        row, _ = self._preference_row(
            "Motion", "Action frames load only while Optimize is running", suffix=self.motion_combo
        )
        appearance_rows.pack_start(row, False, False, 0)
        results = Gtk.Switch()
        results.set_active(bool(self.settings["display"]["show_results"]))
        results.connect("notify::active", self._on_show_results_changed)
        row, _ = self._preference_row("Keep the before-and-after result visible", "Show measured validation after each run", suffix=results)
        appearance_rows.pack_start(row, False, False, 0)
        page.pack_start(appearance, False, False, 0)

        safety, safety_rows = self._section("Safety boundary")
        for title, subtitle, icon in (
            ("Current user only", "No root daemon, sudo path, or system-service control", "system-users-symbolic"),
            ("Transparent reclaim only", "No closing, stopping, freezing, cache dropping, or restart fallback", "security-high-symbolic"),
            ("Aggregate history only", "No filenames, window titles, or command lines are stored", "changes-prevent-symbolic"),
        ):
            row, _ = self._preference_row(title, subtitle, icon=icon)
            safety_rows.pack_start(row, False, False, 0)
        page.pack_start(safety, False, False, 0)

        setup, setup_rows = self._section("Setup and recovery")
        review = Gtk.Button(label="Review")
        review.connect("clicked", self._on_review_setup)
        row, _ = self._preference_row(
            "Review initial setup again", "Automation turns off until setup is completed again", suffix=review
        )
        setup_rows.pack_start(row, False, False, 0)
        row, _ = self._preference_row(
            "Versioned v1 recovery retained",
            "One command can restore the previous app and settings snapshot",
            icon="document-revert-symbolic",
        )
        setup_rows.pack_start(row, False, False, 0)
        page.pack_start(setup, False, False, 0)
        return self._scrolled_page(page)

    def _build_setup_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        css(page, "setup-page", "page")
        heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        css(heading, "setup-heading")
        heading.pack_start(make_label("Set your care boundaries once", style="display-title", wrap=True), False, False, 0)
        heading.pack_start(
            make_label(
                "Legacy close-app approvals were discarded. Review what RAM Relief may park and clean; automation stays off until you finish.",
                style="body-muted",
                wrap=True,
            ),
            False,
            False,
            0,
        )
        page.pack_start(heading, False, False, 0)

        memory = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        css(memory, "setup-card")
        memory.pack_start(make_label("Memory care", style="section-title"), False, False, 0)
        memory.pack_start(
            make_label(
                "Pressure-aware automatic • 10-minute idle history • known user application scopes only • never closes or suspends",
                style="body-muted",
                wrap=True,
            ),
            False,
            False,
            0,
        )
        supported, reason = reclaim_supported()
        memory.pack_start(
            make_label(
                "Transparent reclaim is available on this laptop" if supported else f"Memory care will fail closed: {reason}",
                style="positive-copy" if supported else "warning-copy",
                wrap=True,
            ),
            False,
            False,
            0,
        )
        page.pack_start(memory, False, False, 0)

        cleanup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        css(cleanup, "setup-card")
        cleanup.pack_start(make_label("Cleanup after an Optimize click", style="section-title"), False, False, 0)
        for category in ("thumbnails", "browser_cache", "trash", "npm", "yarn", "pip", "playwright"):
            title, subtitle = CATEGORY_COPY[category]
            toggle = Gtk.Switch()
            toggle.set_active(bool(self.settings["cleanup"]["categories"][category]))
            toggle.connect("notify::active", self._on_cleanup_category_changed, category)
            row, _ = self._preference_row(title, subtitle, suffix=toggle)
            css(row, "setup-option")
            cleanup.pack_start(row, False, False, 0)
            self.category_switches.setdefault(category, []).append(toggle)
        page.pack_start(cleanup, False, False, 0)

        confirmation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        css(confirmation, "setup-confirmation")
        confirmation.pack_start(
            make_label(
                "Enabled cleanup runs only when you click Optimize. The two-minute memory timer never deletes files.",
                style="body-muted",
                wrap=True,
            ),
            True,
            True,
            0,
        )
        complete = Gtk.Button(label="Complete setup")
        css(complete, "optimize-button")
        complete.set_valign(Gtk.Align.CENTER)
        complete.connect("clicked", self._on_complete_setup)
        confirmation.pack_end(complete, False, False, 0)
        page.pack_start(confirmation, False, False, 0)
        return self._scrolled_page(page, width=820)

    def _motion_reduced(self) -> bool:
        mode = self.settings.get("reduced_motion", "system")
        if mode == "reduce":
            return True
        if mode == "full":
            return False
        return not bool(Gtk.Settings.get_default().get_property("gtk-enable-animations"))

    def _detect_system_dark(self) -> bool:
        try:
            interface = Gio.Settings.new("org.gnome.desktop.interface")
            return interface.get_string("color-scheme") == "prefer-dark"
        except GLib.Error:
            return bool(Gtk.Settings.get_default().get_property("gtk-application-prefer-dark-theme"))

    def _on_system_theme_changed(self, settings: Gio.Settings, _key: str) -> None:
        self._system_dark = settings.get_string("color-scheme") == "prefer-dark"
        if self.settings.get("theme") == "system":
            self._apply_theme()

    def _apply_theme(self) -> None:
        theme = self.settings.get("theme", "system")
        prefer_dark = self._system_dark if theme == "system" else theme == "dark"
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", prefer_dark)
        context = self.get_style_context()
        context.remove_class("dark")
        context.remove_class("light")
        context.add_class("dark" if prefer_dark else "light")
        header_context = self.header.get_style_context()
        header_context.remove_class("dark")
        header_context.remove_class("light")
        header_context.add_class("dark" if prefer_dark else "light")

    def _save(self) -> None:
        self.store.data = self.settings
        self.store.save()
        self.settings = self.store.data

    def show_notice(self, text: str) -> None:
        self.notice_label.set_text(text)
        self.notice_revealer.set_reveal_child(True)
        if self.notice_timeout:
            GLib.source_remove(self.notice_timeout)
        self.notice_timeout = GLib.timeout_add_seconds(4, self._hide_notice)

    def _hide_notice(self) -> bool:
        self.notice_revealer.set_reveal_child(False)
        self.notice_timeout = 0
        return False

    def _on_mode_changed(self, combo: Gtk.ComboBoxText) -> None:
        index = combo.get_active()
        if index < 0:
            return
        self.settings["memory_mode"] = MODE_VALUES[index]
        self._save()
        success, detail = sync_timer(self.settings)
        self.timer_subtitle.set_text(timer_state())
        if not success:
            self.show_notice(f"Timer stayed safe and off: {detail or 'unavailable'}")

    def _on_policy_changed(self, combo: Gtk.ComboBoxText, app_id: str) -> None:
        index = combo.get_active()
        if index < 0:
            return
        self.settings["app_policies"][app_id] = POLICY_VALUES[index]
        self._save()
        self.refresh_live_state()

    def _on_cleanup_category_changed(self, row: Gtk.Switch, _param: object, category: str) -> None:
        active = bool(row.get_active())
        self.settings["cleanup"]["categories"][category] = active
        if category == "trash":
            self.settings["cleanup"]["automatic_trash"] = active
        for other in self.category_switches.get(category, []):
            if other is not row and other.get_active() != active:
                other.set_active(active)
        self._save()
        if self.settings["setup_complete"]:
            self._start_cleanup_scan()

    def _on_stale_age_changed(self, spin: Gtk.SpinButton) -> None:
        self.settings["cleanup"]["stale_days"] = int(spin.get_value())
        self._save()

    def _on_theme_changed(self, combo: Gtk.ComboBoxText) -> None:
        index = combo.get_active()
        if index < 0:
            return
        self.settings["theme"] = THEME_VALUES[index]
        self._save()
        self._apply_theme()

    def _on_motion_changed(self, combo: Gtk.ComboBoxText) -> None:
        index = combo.get_active()
        if index >= 0:
            self.settings["reduced_motion"] = MOTION_VALUES[index]
            self._save()

    def _on_show_results_changed(self, row: Gtk.Switch, _param: object) -> None:
        self.settings["display"]["show_results"] = row.get_active()
        self._save()
        if not row.get_active():
            self.result_revealer.set_reveal_child(False)

    def _on_review_setup(self, _button: Gtk.Button) -> None:
        self.settings["setup_complete"] = False
        self._save()
        sync_timer(self.settings)
        self._set_navigation_enabled(False)
        self.show_page("setup", force=True)

    def _on_complete_setup(self, _button: Gtk.Button) -> None:
        self.settings["setup_complete"] = True
        self._save()
        success, detail = sync_timer(self.settings)
        self._set_navigation_enabled(True)
        self.show_page("overview", force=True)
        self.timer_subtitle.set_text(timer_state())
        if success:
            self.show_notice("Setup saved. Memory is pressure-aware; cleanup waits for Optimize.")
        else:
            self.show_notice(f"Setup saved; automatic timer stayed off: {detail or 'unavailable'}")
        self.refresh_live_state()
        self._start_cleanup_scan()

    def _rebuild_exclusion_rows(self) -> None:
        for child in self.exclusion_rows.get_children():
            self.exclusion_rows.remove(child)
        for value in self.settings["cleanup"].get("exclusions", []):
            remove = Gtk.Button(image=make_icon("edit-delete-symbolic"))
            remove.set_relief(Gtk.ReliefStyle.NONE)
            remove.set_tooltip_text("Remove exclusion")
            remove.connect("clicked", self._remove_exclusion, value)
            row, _ = self._preference_row(
                value, "Protected from all RAM Relief cleanup categories", icon="folder-symbolic", suffix=remove
            )
            self.exclusion_rows.pack_start(row, False, False, 0)
        add = Gtk.Button(label="Choose folder")
        add.connect("clicked", self._choose_exclusion)
        row, _ = self._preference_row(
            "Add an exclusion folder", "Local folders only; exclusions never expand cleanup scope", suffix=add
        )
        self.exclusion_rows.pack_start(row, False, False, 0)
        self.exclusion_rows.show_all()

    def _choose_exclusion(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Choose a folder RAM Relief must never clean",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "Choose",
            "Cancel",
        )
        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            path = dialog.get_filename()
            if path:
                absolute = os.path.abspath(path)
                values = self.settings["cleanup"].setdefault("exclusions", [])
                if absolute not in values:
                    values.append(absolute)
                    self._save()
                    self._rebuild_exclusion_rows()
        dialog.destroy()

    def _remove_exclusion(self, _button: Gtk.Button, value: str) -> None:
        values = self.settings["cleanup"].setdefault("exclusions", [])
        self.settings["cleanup"]["exclusions"] = [item for item in values if item != value]
        self._save()
        self._rebuild_exclusion_rows()

    def _periodic_refresh(self) -> bool:
        if not self.busy:
            self.refresh_live_state()
        return True

    def refresh_live_state(self, *, show_notice: bool = False) -> None:
        snapshot = read_memory_snapshot()
        self.hero_memory.set_text(format_bytes(snapshot.available_bytes))
        self.hero_memory_caption.set_text(f"available now • PSI {snapshot.psi_some_avg10:.2f}%")
        if pressure_triggered(snapshot, self.settings):
            self.hero_detail.set_text("Memory pressure is active. RAM Relief will still touch only idle, approved application scopes.")
        else:
            self.hero_detail.set_text("Memory pressure is comfortable. Optimize can still run your approved cleanup.")
        apps = discover_app_cgroups(self.settings)
        potential = 0
        protected = 0
        for app in apps:
            if app.protected_reason or app.policy == POLICY_NEVER or app.app_id is None:
                protected += 1
            elif app.policy in (POLICY_AUTO, POLICY_MANUAL) and app.reading.memory_current >= 192 * 1024 * 1024:
                potential += min(512 * 1024 * 1024, max(64 * 1024 * 1024, int(app.reading.memory_current * 0.35)))
        self.potential_metric[1].set_text(format_bytes(potential) if potential else "None now")
        self.protected_metric[1].set_text(str(protected))
        self._apply_cached_status_rows(apps)
        if show_notice:
            self.show_notice("Current pressure and application scopes refreshed.")

    def _apply_cached_status_rows(self, apps: list[Any]) -> None:
        activity = load_activity()
        samples = activity.get("samples", {})
        grouped: dict[str, list[Any]] = {}
        for item in apps:
            if item.app_id:
                grouped.setdefault(item.app_id, []).append(item)
        for spec in APP_CATALOG:
            subtitle = self.policy_subtitles.get(spec.app_id)
            if subtitle is None:
                continue
            matches = grouped.get(spec.app_id, [])
            if not matches:
                prefix = "Protected" if spec.protected_reason else POLICY_LABELS[self.settings["app_policies"][spec.app_id]]
                subtitle.set_text(f"{prefix} • not running")
                continue
            total = sum(item.reading.memory_current for item in matches)
            if spec.protected_reason:
                status, reason = "Protected", spec.protected_reason
            else:
                cached = [samples.get(item.key, {}) for item in matches]
                statuses = {str(item.get("status", "Busy")) for item in cached}
                status = next((name for name in ("Playing", "Recently restored", "Protected", "Busy", "Idle") if name in statuses), "Busy")
                reason = next(
                    (str(item.get("reason")) for item in cached if item.get("status") == status and item.get("reason")),
                    "Awaiting the next activity sample",
                )
            subtitle.set_text(f"{status} • {format_bytes(total)} • {reason}")

    def _start_cleanup_scan(self, *, show_notice: bool = False) -> None:
        if not self.settings["setup_complete"] and not self.demo:
            return
        self.cleanup_metric[1].set_text("Scanning…")
        self.cleanup_summary_subtitle.set_text("Scanning approved roots without changing files…")
        snapshot = copy.deepcopy(self.settings)
        threading.Thread(
            target=self._cleanup_scan_worker,
            args=(snapshot, show_notice),
            name="ram-relief-cleanup-scan",
            daemon=True,
        ).start()

    def _cleanup_scan_worker(self, settings: Mapping[str, Any], show_notice: bool) -> None:
        try:
            scan = scan_cleanup(settings)
        except Exception as error:
            GLib.idle_add(self._cleanup_scan_failed, str(error))
            return
        GLib.idle_add(self._apply_cleanup_scan, scan, show_notice)

    def _cleanup_scan_failed(self, detail: str) -> bool:
        self.cleanup_metric[1].set_text("Unavailable")
        self.cleanup_summary_subtitle.set_text("Scan failed closed; no files were changed")
        self.show_notice(f"Cleanup scan stayed safe: {detail}")
        return False

    def _apply_cleanup_scan(self, scan: CleanupScan, show_notice: bool = False) -> bool:
        self.cleanup_scan = scan
        self.cleanup_metric[1].set_text(format_bytes(scan.total_bytes))
        self.cleanup_summary_subtitle.set_text(
            f"{scan.total_count} revalidatable items • {format_bytes(scan.total_bytes)} • no filenames stored"
        )
        if show_notice:
            self.show_notice(f"Detailed scan found {format_bytes(scan.total_bytes)} of approved cleanup.")
        return False

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.optimize_button.set_sensitive(not busy)
        if busy:
            self.optimize_icon.hide()
            self.optimize_spinner.show()
            self.optimize_spinner.start()
            self.optimize_label.set_text("Optimizing…")
            self.result_revealer.set_reveal_child(False)
            self.animator.start("memory")
        else:
            self.optimize_spinner.stop()
            self.optimize_spinner.hide()
            self.optimize_icon.show()
            self.optimize_label.set_text("Optimize")
            self.animator.stop(show_static=True)

    def _on_optimize(self, _button: Gtk.Button) -> None:
        if self.busy:
            return
        if not self.settings["setup_complete"] and not self.demo:
            self.show_page("setup", force=True)
            return
        if self.demo:
            self.start_demo()
            return
        self._set_busy(True)
        snapshot = copy.deepcopy(self.settings)
        threading.Thread(
            target=self._optimize_worker,
            args=(snapshot,),
            name="ram-relief-optimize",
            daemon=True,
        ).start()

    def _optimize_worker(self, settings: Mapping[str, Any]) -> None:
        from ram_relief_core import run_memory_relief

        memory = run_memory_relief(settings, automatic=False)
        append_aggregate_history({"kind": "memory", **memory.to_aggregate_dict()})
        GLib.idle_add(self.animator.start, "cleanup")
        cleanup = run_cleanup(settings)
        GLib.idle_add(self._finish_optimize, memory, cleanup)

    def _finish_optimize(self, memory: MemoryRunResult, cleanup: CleanupResult) -> bool:
        self._set_busy(False)
        memory_reduction = memory.actual_memory_reduction_bytes
        available_change = memory.available_change_bytes
        swap_growth = memory.swap_growth_bytes
        cleanup_bytes = cleanup.deleted_bytes
        if memory.error and not memory.parked:
            title = "Storage care finished; memory stayed untouched"
            detail = f"Memory failed closed: {memory.error}. Cleanup removed {format_bytes(cleanup_bytes)} from approved roots."
            icon = "dialog-warning-symbolic"
        elif memory_reduction == 0 and cleanup_bytes == 0:
            title = "Everything is already in a safe range"
            detail = memory.stopped_reason or "No approved stale items or safely eligible app memory needed attention."
            icon = "emblem-ok-symbolic"
        else:
            title = "Optimization complete"
            detail = (
                f"Measured {format_bytes(memory_reduction)} less memory in reclaimed app scopes and "
                f"cleared {format_bytes(cleanup_bytes)} from revalidated cleanup entries."
            )
            icon = "emblem-ok-symbolic"
        skipped = len([item for item in memory.decisions if not item.eligible]) + sum(cleanup.skip_reasons.values())
        if skipped:
            detail += f" {skipped} protected or changed items were skipped."
        self.result_title.set_text(title)
        self.result_detail.set_text(detail)
        self.result_icon.set_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        self.result_values["memory"].set_text(format_bytes(memory_reduction))
        self.result_values["available"].set_text(f"{available_change / (1024 * 1024):+.1f} MiB")
        self.result_values["swap"].set_text(f"{swap_growth / (1024 * 1024):+.1f} MiB")
        self.result_values["cleanup"].set_text(format_bytes(cleanup_bytes))
        if self.settings["display"]["show_results"]:
            self.result_revealer.set_reveal_child(True)
        else:
            self.show_notice(title)
        self.refresh_live_state()
        self._start_cleanup_scan()
        return False

    def start_demo(self) -> bool:
        if self.busy:
            return False
        self._set_busy(True)
        self.hero_detail.set_text("Measuring pressure, eligible app scopes, and approved cleanup…")
        try:
            duration = int(os.environ.get("RAM_RELIEF_QA_DEMO_MS", "1500"))
        except ValueError:
            duration = 1500
        duration = max(500, min(10000, duration))
        GLib.timeout_add(max(250, duration // 2), self._start_demo_cleanup_phase)
        GLib.timeout_add(duration, self._finish_demo)
        return False

    def _start_demo_cleanup_phase(self) -> bool:
        if self.busy:
            self.animator.start("cleanup")
            self.hero_detail.set_text("Revalidating the approved cleanup manifest before any storage care…")
        return False

    def _finish_demo(self) -> bool:
        self._set_busy(False)
        self.hero_detail.set_text("Preview complete. Every protection was checked without changing app memory or files.")
        self.result_title.set_text("Preview complete — your work stayed open")
        self.result_detail.set_text(
            "The visual demo changed no files or app memory. A real Optimize run always reports measured results."
        )
        self.result_icon.set_from_icon_name("emblem-ok-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        for label in self.result_values.values():
            label.set_text("0 B")
        self.result_revealer.set_reveal_child(True)
        return False


class RamReliefApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window: RamReliefWindow | None = None
        self.pending_page = "overview"
        self.pending_demo = False

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        if STYLE_PATH.exists():
            provider = Gtk.CssProvider()
            provider.load_from_path(str(STYLE_PATH))
            screen = Gdk.Screen.get_default()
            if screen:
                Gtk.StyleContext.add_provider_for_screen(
                    screen,
                    provider,
                    Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
                )

    def do_activate(self) -> None:
        if self.window is None:
            self.window = RamReliefWindow(self, demo=self.pending_demo)
        self.window.show_page(self.pending_page, force=self.pending_demo)
        self.window.present()
        if self.pending_demo:
            GLib.timeout_add(350, self.window.start_demo)
            self.pending_demo = False

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        arguments = list(command_line.get_arguments())[1:]
        if "--configure" in arguments:
            self.pending_page = "settings"
        elif "--memory" in arguments:
            self.pending_page = "memory"
        elif "--cleanup" in arguments:
            self.pending_page = "cleanup"
        else:
            self.pending_page = "overview"
        self.pending_demo = "--demo" in arguments
        self.activate()
        return 0


__all__ = ["RamReliefApplication", "RamReliefWindow", "sync_timer", "timer_state"]
