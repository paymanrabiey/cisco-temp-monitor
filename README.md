# Cisco Temperature Monitoring

ابزار مانیتورینگ تجهیزات سیسکو با تمرکز اصلی روی دمای دستگاه‌ها، همراه با قابلیت‌های اضافی برای مانیتورینگ منابع، اینونتوری SNMP، پشتیبان‌گیری کانفیگ و حالت شبیه‌سازی.

A Cisco network device monitoring tool focused on temperature monitoring, with additional features for CPU/RAM/PoE, SNMP inventory, physical port view, network discovery, configuration backup, and simulation mode.

---

## Features | قابلیت‌ها

- Temperature monitoring with ~60 second polling
- CPU / RAM / Uptime / PoE monitoring (default 10-minute interval)
- SNMP Inventory (static info, interfaces, ports, stack, CDP/LLDP)
- Physical port view for Switch and Stack
- Network Discovery via SNMP
- Config Backup (Running & Startup) via SSH
- Config version history and Diff
- Simulation Mode (test without real devices)
- Modern UI built with CustomTkinter
- Non-blocking polling using Thread / ThreadPoolExecutor
- Save state in `devices.json`
- Customizable Detail Card order
- Monitor Strip mode (NOC-style view)

---

## Requirements | پیش‌نیازها

- Python 3.10 or higher
- Windows (tested on Windows 10/11)

Install dependencies:

```bash
pip install -r requirements.txt




## How to Run | نحوه اجرا

Edit devices.json and add your devices (or use Simulation mode).
Run the application:

Bashpython main.py
Or use the provided batch file:
Bashrun.bat

## Configuration | تنظیمات
Main configuration file: devices.json
Important fields:

host: IP address of the device
community: SNMP community string
ssh_user / ssh_password: Credentials for config backup
simulate: Set to true for testing without real devices
sim_base: Base temperature used in simulation mode

Security Note:
Never commit real credentials, community strings, or production device information to the repository.

## Simulation Mode | حالت شبیه‌سازی
For testing without real Cisco devices, set "simulate": true in devices.json.
The application will generate realistic temperature and status data.

##Project Structure | ساختار پروژه
textcisco-temp-monitor/
├── main.py                 # Main application
├── snmp_temp.py            # Temperature monitoring
├── snmp_inventory.py       # SNMP inventory
├── config_backup.py        # SSH config backup
├── discover.py             # Network discovery
├── devices.json            # Device configuration & state
├── requirements.txt
├── run.bat
└── VERSION

##Disclaimer | سلب مسئولیت
This tool is intended for educational and internal network monitoring purposes.
Use it responsibly and only on devices you own or have permission to monitor.

##Author
Developed for internal use in monitoring Cisco devices.
text---

### چطور اضافه کنی؟

**روش ۱ (پیشنهادی):**
1. در GitHub Desktop روی فایل `README.md` دابل‌کلیک کن (یا از طریق Show in Explorer بازش کن).
2. کل محتوای قبلی رو پاک کن و متن بالا رو جایگزین کن.
3. ذخیره کن.
4. برگرد به GitHub Desktop → پیام کامیت بنویس (مثلاً `Add bilingual README`) → Commit → Push origin.

---