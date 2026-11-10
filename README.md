# RAM Relief 2.0

Public source edition with original illustrations. Run `python3 -m unittest discover -s tests` to exercise the synthetic, read-only test fixtures. Review the installer and local settings before enabling maintenance actions.


**RAM Relief — Memory & Storage Care** is a native Ubuntu utility that makes room without closing applications.

## What it does

- Reads `MemAvailable` and memory PSI instead of treating ordinary Linux cache use as a problem.
- Writes only to a known, user-owned graphical application scope's cgroup v2 `memory.reclaim` file.
- Requests anonymous-page reclaim with `swappiness=max`, so idle pages can move to swap while the application remains running.
- Measures the real cgroup memory reduction, swap change, available-memory change, and every skip reason.
- Cleans only fixed cache roots and local Freedesktop Trash entries selected during setup.
- Stores activity samples and aggregate history atomically with mode `0600`; it stores no filenames, command lines, or window titles.

It has no process-closing path, process-freezing path, cache-drop path, restart fallback, root daemon, or sudo route.

## Default policy

- **Auto:** Zen, Firefox, Brave, Discord, Slack, Teams for Linux, Telegram, Obsidian.
- **Allow manually:** Spotify, Steam, Thunderbird, VLC, Visual Studio Code.
- **Always protected:** Chrome, Chromium, Codex/ChatGPT, terminals, file managers, remote-desktop tools, sync/backup clients, password managers, VPNs, desktop infrastructure, services, and unknown applications.

Automatic memory checks run as a non-resident systemd user timer every two minutes. They require real pressure plus a ten-minute low-activity history, no playback, no matching inhibitor, at least 192 MiB in the application scope, available swap above the reserve, and no cooldown.

Cleanup never runs from the timer. It runs only after the user completes setup and explicitly clicks **Optimize**.

## Install and rollback

```bash
./install-v2
```

The verified v1 baseline is retained at:

```text
```

After installation, one command restores v1 and its original settings:

```bash
```

## Command line

```text
ram-relief                  Open the native app
ram-relief --configure      Open Settings
ram-relief --status         Show pressure, swap, setup, and reclaim capability
ram-relief --list-candidates
                            Show application-scope park eligibility and reasons
ram-relief --dry-run        Preview memory and cleanup work without changing data
ram-relief --scan-json      Print aggregate cleanup scan JSON without filenames
```

`--auto-run` is reserved for the user timer and performs memory care only.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 tests/verify_memory_reclaim.py
```

The live verification allocates checksum-known anonymous memory in a disposable user scope, reclaims part of it toward swap, confirms the process stayed alive, faults the pages back in, and verifies the checksum is unchanged. It never targets a production application.
