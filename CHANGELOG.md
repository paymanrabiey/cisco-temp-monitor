# Changelog

All notable changes to this project are documented here.  
Version numbers follow [Semantic Versioning](https://semver.org/) and the `VERSION` file at the repo root.

## [1.0.0] — 2026-08-20

First stable release — **Cisco Temp Monitor** for NOC / internal monitoring.

### Added

- Temperature monitoring (SNMPv2c), default poll ~60s
- CPU / RAM / Uptime / PoE dynamic metrics (~10 min)
- Compact **Monitor Strip** mode — thin bar docked top-right for multi-monitor NOC walls
- Full management mode — device list + ranking panel side by side
- SNMP inventory: static info, interfaces, physical ports, stack, CDP/LLDP
- Cisco-like physical port faceplate (24/48-port uplink separation)
- Reorderable detail-page cards (persisted in `devices.json`)
- Network discovery (SNMP scan)
- **Config backup** via SSH: running + startup configs, timestamped versions, unified diffs
- Scheduled config backup (hours interval, configurable)
- Config browser UI — view history, compare versions, open backup folder
- Simulation mode for testing without real devices
- PyInstaller build (`build.bat`) and release archive (`releases/vX.Y.Z/`)
- Version display in application title bar

### Stack

- Python 3.10+
- CustomTkinter, pysnmp, paramiko, Pillow

[1.0.0]: https://github.com/paymanrabiey/cisco-temp-monitor/releases/tag/v1.0.0
