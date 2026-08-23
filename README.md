# Cisco Temp Monitor

[![Version](https://img.shields.io/badge/version-1.0.0-blue)](VERSION)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)](https://github.com/paymanrabiey/cisco-temp-monitor)

ابزار دسکتاپ **مانیتورینگ دمای سوییچ و روتر سیسکو** — مناسب NOC و مانیتورهای بزرگ سازمانی.  
SNMP برای دما و منابع، SSH برای بکاپ کانفیگ، و حالت نوار باریک کنار صفحه.

A Windows desktop tool for **Cisco temperature monitoring** with SNMP inventory, physical port view, config backup (running/startup), and a NOC-friendly strip layout.

**Repository:** [github.com/paymanrabiey/cisco-temp-monitor](https://github.com/paymanrabiey/cisco-temp-monitor)

---

## نسخه فعلی | Current release

| Item | Value |
|------|--------|
| Version | **1.0.0** (see [VERSION](VERSION)) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |
| First publish | August 2026 |

> این ریپو نقطه شروع کنترل نسخه است. برای هر انتشار جدید عدد `VERSION` را بالا ببرید، `build.bat` بزنید و در GitHub یک **Release** با tag `vX.Y.Z` بسازید.

---

## قابلیت‌ها | Features

| Area | Description |
|------|-------------|
| **Temperature** | SNMP polling (~60s), color-coded ranking (normal → critical) |
| **CPU / RAM / PoE** | Dynamic metrics (~10 min interval) |
| **Monitor Strip** | Narrow top-right bar — stays on top for NOC walls; expand left for full UI |
| **SNMP inventory** | Model, serial, IOS, interfaces, stack, CDP/LLDP |
| **Port faceplate** | Physical layout; 24-port (25+ uplink) and 48-port (49+ uplink) |
| **Discovery** | SNMP network scan to add devices |
| **Config backup** | SSH `show running-config` / `show startup-config`, versioned under `config/` |
| **Diff** | Running vs startup, running vs previous snapshot |
| **Simulation** | Test UI without real network access |

---

## پیش‌نیازها | Requirements

- Windows 10 / 11
- Python **3.10+** (for running from source)
- Network access to devices: **SNMPv2c** (monitoring), **SSH** (config backup only)

### Install dependencies

```bash
pip install -r requirements.txt
```

---

## اجرا | Run

```bash
python main.py
```

Or double-click `run.bat`.

On first run, edit `devices.json` (or use **Simulation** / **Discovery** from the app).

---

## استقرار روی سرور مانیتورینگ | Deploy on monitoring PC

1. Build or download the executable:
   ```bash
   build.bat
   ```
   Output: `dist\CiscoTempMonitor.exe` (~28 MB)

2. Copy to the monitoring server:
   - `CiscoTempMonitor.exe`
   - optional: `devices.json` (your local config — **do not use the sample from GitHub in production**)

3. Run the EXE. Next to it the app creates:
   - `devices.json` — device list & settings (if missing)
   - `config\` — per-device config backups

4. Default UI opens in **Strip mode** (top-right). Use **◂ باز** to open full management.

---

## ساخت EXE و آرشیو نسخه | Build & release

```bash
build.bat          # EXE + copy to releases/v{VERSION}/
make_release.bat   # same as build.bat
```

Release layout:

```
releases/
  v1.0.0/
    CiscoTempMonitor.exe
    VERSION
    NOTES.txt
    SOURCE/          # snapshot of .py sources at build time
```

---

## تنظیمات | Configuration

File: **`devices.json`** (created/updated by the app)

| Field | Purpose |
|-------|---------|
| `host` | Device IP |
| `community` | SNMP community |
| `ssh_user` / `ssh_password` | Config backup (SSH) |
| `simulate` | `true` = no real SNMP/SSH |
| `monitor_strip_mode` | `true` = NOC strip (default) |
| `config_backup_hours` | Auto backup interval; `0` = off |

Sample file in repo is **example only** (`simulate: true`, fake IPs).

---

## امنیت | Security

**Do not commit production secrets.**

- Replace sample `devices.json` locally; avoid pushing real community strings or SSH passwords.
- Folder `config/` contains full device configs — kept out of git via `.gitignore`.
- Use a **private** GitHub repo if the codebase must stay internal.

---

## ساختار پروژه | Project layout

```
main.py              Main UI (CustomTkinter)
snmp_temp.py         Temperature via SNMP
snmp_inventory.py    Inventory, ports, CDP, stack
config_backup.py     SSH backup & diffs
discover.py          Network discovery
devices.json         Runtime state (local)
config/              Config backups (local, gitignored)
VERSION              Single source of truth for version string
```

---

## مشارکت و نسخه بعدی | Contributing & next version

1. Change `VERSION` (e.g. `1.0.1`)
2. Update [CHANGELOG.md](CHANGELOG.md)
3. Commit & push to `main`
4. Create GitHub Release: tag `v1.0.1`, attach `CiscoTempMonitor-v1.0.1.exe` from `dist/`

```bash
git add .
git commit -m "Release v1.0.1"
git push origin main
git tag v1.0.1
git push origin v1.0.1
```

---

## Disclaimer

For **educational and authorized internal monitoring** only.  
Monitor only devices you own or are permitted to access.

---

## Author

**Payman Rabiey** — [github.com/paymanrabiey](https://github.com/paymanrabiey)

Developed for Cisco temperature monitoring in operational / NOC environments.
