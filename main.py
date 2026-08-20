"""
Cisco Temperature Monitor
مانیتورینگ دمای سوییچ و روترهای سیسکو
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import customtkinter as ctk
from tkinter import Canvas, messagebox, ttk

from discover import DiscoveredHost, discover_networks, iter_hosts, parse_networks
from config_backup import (
    backup_device_config,
    compare_versions,
    list_versions,
    open_device_config_folder,
    read_version_file,
)
from snmp_inventory import (
    TYPE_BADGE,
    TYPE_LABELS,
    dynamic_rows,
    fetch_cdp,
    fetch_dynamic,
    fetch_interfaces,
    fetch_ports,
    fetch_stack,
    fetch_static,
    static_rows,
)
from snmp_temp import TempReading, read_cisco_temperature

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "devices.json"


def read_app_version() -> str:
    for candidate in (APP_DIR / "VERSION", Path(__file__).resolve().parent / "VERSION"):
        try:
            if candidate.exists():
                return candidate.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except Exception:
            pass
    return "1.0.0"


APP_VERSION = read_app_version()

# Defaults
TEMP_POLL_SEC = 60          # temperature / light monitor
DYNAMIC_POLL_SEC = 600      # CPU / mem / uptime (10 min)
CONFIG_BACKUP_HOURS = 24    # 0 = disabled
POLL_INTERVAL_SEC = TEMP_POLL_SEC

# Thresholds (°C)
TEMP_OK = 45
TEMP_WARN = 55
TEMP_CRIT = 65

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

COLORS = {
    "bg": "#0f1419",
    "panel": "#1a2332",
    "panel2": "#243044",
    "border": "#2d3a4f",
    "text": "#e8eef7",
    "muted": "#8b9bb4",
    "accent": "#3d9cf0",
    "ok": "#2ecc71",
    "warn": "#f1c40f",
    "hot": "#e67e22",
    "crit": "#e74c3c",
    "offline": "#6c7a89",
}


@dataclass
class Device:
    id: str
    name: str
    host: str
    community: str = "public"
    port: int = 161
    simulate: bool = False
    temperature: Optional[float] = None
    status: str = "pending"  # pending | online | offline | nosensor
    last_error: str = ""
    sim_base: float = 40.0
    device_type: str = "unknown"
    static_info: dict = field(default_factory=dict)
    dynamic_info: dict = field(default_factory=dict)
    static_fetched_at: float = 0.0
    dynamic_fetched_at: float = 0.0
    # SSH for config backup (running / startup)
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_port: int = 22
    ssh_enable: str = ""
    config_backed_up_at: float = 0.0


DEFAULT_DETAIL_CARD_ORDER = [
    "ports",
    "cpu_mem",
    "dashboard",
    "inventory",
    "iface",
    "cdp",
]

DETAIL_CARD_LABELS = {
    "ports": "نمای فیزیکی پورت‌ها",
    "cpu_mem": "CPU & Memory",
    "dashboard": "دما / سیستم / PoE",
    "inventory": "Full Inventory",
    "iface": "Interface Inventory",
    "cdp": "همسایگان CDP / LLDP",
}


@dataclass
class AppState:
    devices: list[Device] = field(default_factory=list)
    temp_poll_seconds: int = TEMP_POLL_SEC
    dynamic_poll_seconds: int = DYNAMIC_POLL_SEC
    detail_card_order: list[str] = field(
        default_factory=lambda: list(DEFAULT_DETAIL_CARD_ORDER)
    )
    # True = thin right-edge strip (default for NOC monitors)
    monitor_strip_mode: bool = True
    config_backup_hours: int = CONFIG_BACKUP_HOURS

    @property
    def poll_seconds(self) -> int:
        """Backward-compatible alias used by older UI labels."""
        return self.temp_poll_seconds

    def normalized_detail_order(self) -> list[str]:
        order = [k for k in self.detail_card_order if k in DETAIL_CARD_LABELS]
        for k in DEFAULT_DETAIL_CARD_ORDER:
            if k not in order:
                order.append(k)
        return order


def temp_color(temp: Optional[float], online: bool) -> str:
    if not online or temp is None:
        return COLORS["offline"]
    if temp >= TEMP_CRIT:
        return COLORS["crit"]
    if temp >= TEMP_WARN:
        return COLORS["hot"]
    if temp >= TEMP_OK:
        return COLORS["warn"]
    return COLORS["ok"]


def temp_bg(temp: Optional[float], online: bool) -> str:
    """Soft background tint for ranking cards."""
    if not online or temp is None:
        return "#243044"
    if temp >= TEMP_CRIT:
        return "#5a2222"
    if temp >= TEMP_WARN:
        return "#5a3a1a"
    if temp >= TEMP_OK:
        return "#4a4518"
    return "#1a3d2e"


def temp_pct(temp: Optional[float]) -> Optional[float]:
    """Map temperature to 0–100% of critical threshold for display."""
    if temp is None:
        return None
    return round(min(100.0, max(0.0, (temp / TEMP_CRIT) * 100.0)), 0)


def metric_text(device: Device) -> str:
    dyn = device.dynamic_info or {}
    t = device.temperature
    tp = temp_pct(t)
    cpu = dyn.get("cpu_percent")
    ram = dyn.get("memory_percent")
    poe_w = dyn.get("poe_used_w")
    poe = dyn.get("poe_percent")
    parts = []
    if t is not None:
        parts.append(f"Temp {t:.0f}° ({tp:.0f}%)" if tp is not None else f"Temp {t:.0f}°")
    else:
        parts.append("Temp —")
    parts.append(f"CPU {cpu}%" if cpu is not None else "CPU —")
    parts.append(f"RAM {ram}%" if ram is not None else "RAM —")
    if poe_w is not None:
        parts.append(f"PoE {poe_w:.0f}W")
    elif poe is not None:
        parts.append(f"PoE {poe}%")
    else:
        parts.append("PoE —")
    return "  ·  ".join(parts)


PORT_COLORS = {
    "up": "#00c853",
    "down": "#8b97a3",
    "disabled": "#6b7680",
}

PORT_PANEL = {
    "bg": "#cfdce6",
    "jack": "#9aa5b1",
    "text": "#1a2330",
    "muted": "#4a5560",
    "active": "#1e88e5",
}


def pct_color(value: Optional[float]) -> str:
    if value is None:
        return COLORS["offline"]
    if value >= 80:
        return COLORS["crit"]
    if value >= 60:
        return COLORS["hot"]
    if value >= 40:
        return COLORS["warn"]
    return COLORS["ok"]


def temp_label(temp: Optional[float], status: str) -> str:
    if status == "offline":
        return "قطع"
    if status == "nosensor":
        return "بدون سنسور"
    if status == "pending" or temp is None:
        return "—"
    return f"{temp:.1f}°C"


def status_text(device: Device) -> str:
    if device.status == "online":
        return "آنلاین"
    if device.status == "nosensor":
        msg = device.last_error or "سنسور دما در SNMP یافت نشد"
        return f"آنلاین · {msg}"
    if device.status == "offline":
        if device.last_error:
            return f"آفلاین — {device.last_error[:70]}"
        return "آفلاین / SNMP در دسترس نیست"
    return "در انتظار پایش…"



def load_state() -> AppState:
    if not CONFIG_PATH.exists():
        return AppState()
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        devices: list[Device] = []
        allowed = {
            "id",
            "name",
            "host",
            "community",
            "port",
            "simulate",
            "sim_base",
            "device_type",
            "static_info",
            "static_fetched_at",
            "ssh_user",
            "ssh_password",
            "ssh_port",
            "ssh_enable",
            "config_backed_up_at",
        }
        for item in raw.get("devices", []):
            payload = {k: v for k, v in item.items() if k in allowed}
            if "id" not in payload or "name" not in payload or "host" not in payload:
                continue
            devices.append(Device(**payload))
        for d in devices:
            d.temperature = None
            d.status = "pending"
            d.last_error = ""
            d.dynamic_info = {}
            d.dynamic_fetched_at = 0.0
        # migrate old poll_seconds → temp_poll_seconds
        temp_poll = int(raw.get("temp_poll_seconds", raw.get("poll_seconds", TEMP_POLL_SEC)))
        dyn_poll = int(raw.get("dynamic_poll_seconds", DYNAMIC_POLL_SEC))
        cfg_hours = int(raw.get("config_backup_hours", CONFIG_BACKUP_HOURS))
        card_order = raw.get("detail_card_order") or list(DEFAULT_DETAIL_CARD_ORDER)
        if not isinstance(card_order, list):
            card_order = list(DEFAULT_DETAIL_CARD_ORDER)
        # Strip mode default; migrate old drawer key if present
        if "monitor_strip_mode" in raw:
            strip_mode = bool(raw.get("monitor_strip_mode"))
        else:
            # old drawer_open meant expanded management — invert to strip
            strip_mode = not bool(raw.get("devices_drawer_open", False))
            # first run / no key → prefer strip
            if "devices_drawer_open" not in raw:
                strip_mode = True
        state = AppState(
            devices=devices,
            temp_poll_seconds=max(15, temp_poll),
            dynamic_poll_seconds=max(60, dyn_poll),
            detail_card_order=[str(x) for x in card_order],
            monitor_strip_mode=strip_mode,
            config_backup_hours=max(0, cfg_hours),
        )
        state.detail_card_order = state.normalized_detail_order()
        return state
    except Exception:
        return AppState()


def save_state(state: AppState) -> None:
    payload = {
        "temp_poll_seconds": state.temp_poll_seconds,
        "dynamic_poll_seconds": state.dynamic_poll_seconds,
        "poll_seconds": state.temp_poll_seconds,
        "detail_card_order": state.normalized_detail_order(),
        "monitor_strip_mode": bool(state.monitor_strip_mode),
        "config_backup_hours": int(state.config_backup_hours),
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "host": d.host,
                "community": d.community,
                "port": d.port,
                "simulate": d.simulate,
                "sim_base": d.sim_base,
                "device_type": d.device_type,
                "static_info": d.static_info,
                "static_fetched_at": d.static_fetched_at,
                "ssh_user": d.ssh_user,
                "ssh_password": d.ssh_password,
                "ssh_port": d.ssh_port,
                "ssh_enable": d.ssh_enable,
                "config_backed_up_at": d.config_backed_up_at,
            }
            for d in state.devices
        ],
    }
    CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def format_ts(ts: float) -> str:
    if not ts:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


class DeviceDialog(ctk.CTkToplevel):
    def __init__(self, master, title: str, device: Optional[Device] = None):
        super().__init__(master)
        self.title(title)
        self.geometry("480x640")
        self.minsize(440, 560)
        self.resizable(False, True)
        self.configure(fg_color=COLORS["panel"])
        self.result: Optional[dict] = None
        self.transient(master)
        self.grab_set()

        btns = ctk.CTkFrame(self, fg_color=COLORS["panel2"], height=64, corner_radius=0)
        btns.pack(side="bottom", fill="x")
        btns.pack_propagate(False)
        ctk.CTkButton(
            btns,
            text="انصراف",
            width=100,
            height=36,
            fg_color="#3a4558",
            command=self.destroy,
        ).pack(side="left", padx=16, pady=12)
        ctk.CTkButton(
            btns,
            text="ذخیره",
            width=140,
            height=36,
            fg_color=COLORS["ok"],
            hover_color="#27ae60",
            text_color="#04140a",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._ok,
        ).pack(side="right", padx=16, pady=12)

        form = ctk.CTkScrollableFrame(self, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        ctk.CTkLabel(
            form, text=title, font=ctk.CTkFont(size=18, weight="bold")
        ).pack(anchor="e", padx=12, pady=(8, 8))

        self.name_var = ctk.StringVar(value=device.name if device else "")
        self.host_var = ctk.StringVar(value=device.host if device else "")
        self.comm_var = ctk.StringVar(value=device.community if device else "public")
        self.port_var = ctk.StringVar(value=str(device.port if device else 161))
        self.sim_var = ctk.BooleanVar(value=device.simulate if device else False)
        self.ssh_user_var = ctk.StringVar(value=device.ssh_user if device else "")
        self.ssh_pass_var = ctk.StringVar(value=device.ssh_password if device else "")
        self.ssh_port_var = ctk.StringVar(
            value=str(device.ssh_port if device else 22)
        )
        self.ssh_enable_var = ctk.StringVar(value=device.ssh_enable if device else "")

        self._field(form, "نام دستگاه", self.name_var)
        self._field(form, "IP / Hostname", self.host_var)
        self._field(form, "SNMP Community", self.comm_var)
        self._field(form, "پورت SNMP", self.port_var)

        ctk.CTkCheckBox(
            form,
            text="حالت شبیه‌سازی (SNMP/SSH واقعی لازم نیست)",
            variable=self.sim_var,
            font=ctk.CTkFont(size=13),
        ).pack(padx=12, pady=10, anchor="e")

        ctk.CTkLabel(
            form,
            text="SSH — بکاپ Running / Startup Config",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="e",
        ).pack(fill="x", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            form,
            text="برای بکاپ کانفیگ لازم است (در حالت شبیه‌سازی اختیاری).",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=11),
            anchor="e",
        ).pack(fill="x", padx=12)

        self._field(form, "SSH Username", self.ssh_user_var)
        self._field(form, "SSH Password", self.ssh_pass_var, show="•")
        self._field(form, "SSH Port", self.ssh_port_var)
        self._field(form, "Enable Secret (اختیاری)", self.ssh_enable_var, show="•")

        self.after(50, self._center)
        self.name_entry.focus_set()
        self.bind("<Return>", lambda _e: self._ok())

    def _field(
        self, parent, label: str, var: ctk.StringVar, show: Optional[str] = None
    ) -> None:
        ctk.CTkLabel(parent, text=label, text_color=COLORS["muted"], anchor="e").pack(
            fill="x", padx=12, pady=(10, 2)
        )
        entry = ctk.CTkEntry(
            parent, textvariable=var, justify="right", height=34, show=show or ""
        )
        entry.pack(fill="x", padx=12)
        if label == "نام دستگاه":
            self.name_entry = entry

    def _center(self) -> None:
        self.update_idletasks()
        if self.master.winfo_ismapped():
            x = self.master.winfo_rootx() + (
                self.master.winfo_width() - self.winfo_width()
            ) // 2
            y = self.master.winfo_rooty() + (
                self.master.winfo_height() - self.winfo_height()
            ) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _ok(self) -> None:
        name = self.name_var.get().strip()
        host = self.host_var.get().strip()
        community = self.comm_var.get().strip() or "public"
        try:
            port = int(self.port_var.get().strip())
            ssh_port = int(self.ssh_port_var.get().strip() or "22")
        except ValueError:
            messagebox.showerror("خطا", "پورت باید عدد باشد.", parent=self)
            return
        if not name or not host:
            messagebox.showerror("خطا", "نام و آدرس دستگاه الزامی است.", parent=self)
            return
        self.result = {
            "name": name,
            "host": host,
            "community": community,
            "port": port,
            "simulate": bool(self.sim_var.get()),
            "ssh_user": self.ssh_user_var.get().strip(),
            "ssh_password": self.ssh_pass_var.get(),
            "ssh_port": ssh_port,
            "ssh_enable": self.ssh_enable_var.get(),
        }
        self.destroy()


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master, app_data: AppState):
        super().__init__(master)
        self.title("تنظیمات پایش")
        self.geometry("460x420")
        self.minsize(420, 380)
        self.resizable(False, False)
        self.configure(fg_color=COLORS["panel"])
        self.result: Optional[dict] = None
        self.transient(master)
        self.grab_set()

        btns = ctk.CTkFrame(self, fg_color=COLORS["panel2"], height=64, corner_radius=0)
        btns.pack(side="bottom", fill="x")
        btns.pack_propagate(False)
        ctk.CTkButton(
            btns, text="انصراف", width=100, height=36, fg_color="#3a4558", command=self.destroy
        ).pack(side="left", padx=16, pady=12)
        ctk.CTkButton(
            btns,
            text="ذخیره",
            width=140,
            height=36,
            fg_color=COLORS["ok"],
            hover_color="#27ae60",
            text_color="#04140a",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self._ok,
        ).pack(side="right", padx=16, pady=12)

        ctk.CTkLabel(
            self, text="تنظیمات اینتروال", font=ctk.CTkFont(size=18, weight="bold")
        ).pack(anchor="e", padx=20, pady=(18, 8))

        self.temp_var = ctk.StringVar(value=str(app_data.temp_poll_seconds))
        self.dyn_var = ctk.StringVar(value=str(app_data.dynamic_poll_seconds))
        self.cfg_var = ctk.StringVar(value=str(app_data.config_backup_hours))

        self._field("بازه مانیتور دما (ثانیه) — پیش‌فرض ۶۰", self.temp_var)
        self._field(
            "بازه اطلاعات متغیر CPU/RAM/Uptime/PoE (ثانیه) — پیش‌فرض ۶۰۰",
            self.dyn_var,
        )
        self._field(
            "بکاپ خودکار کانفیگ (ساعت) — ۰ = خاموش · پیش‌فرض ۲۴",
            self.cfg_var,
        )

        ctk.CTkLabel(
            self,
            text="بکاپ کانفیگ از SSH گرفته می‌شود و در پوشه config ذخیره می‌گردد.",
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=12),
        ).pack(anchor="e", padx=20, pady=(12, 0))

        self.bind("<Return>", lambda _e: self._ok())

    def _field(self, label: str, var: ctk.StringVar) -> None:
        ctk.CTkLabel(self, text=label, text_color=COLORS["muted"], anchor="e").pack(
            fill="x", padx=20, pady=(10, 2)
        )
        ctk.CTkEntry(self, textvariable=var, justify="center", height=36).pack(
            fill="x", padx=20
        )

    def _ok(self) -> None:
        try:
            temp_s = int(self.temp_var.get().strip())
            dyn_s = int(self.dyn_var.get().strip())
            cfg_h = int(self.cfg_var.get().strip())
        except ValueError:
            messagebox.showerror("خطا", "مقادیر باید عدد باشند.", parent=self)
            return
        if temp_s < 15:
            messagebox.showerror("خطا", "بازه دما حداقل ۱۵ ثانیه باشد.", parent=self)
            return
        if dyn_s < 60:
            messagebox.showerror(
                "خطا", "بازه اطلاعات متغیر حداقل ۶۰ ثانیه باشد.", parent=self
            )
            return
        if cfg_h < 0:
            messagebox.showerror("خطا", "بازه بکاپ کانفیگ نمی‌تواند منفی باشد.", parent=self)
            return
        self.result = {
            "temp_poll_seconds": temp_s,
            "dynamic_poll_seconds": dyn_s,
            "config_backup_hours": cfg_h,
        }
        self.destroy()


class ConfigBrowserDialog(ctk.CTkToplevel):
    """Browse timestamped configs, view files, compare versions."""

    FILES = [
        ("running-config.cfg", "Running"),
        ("startup-config.cfg", "Startup"),
        ("diff-running-vs-startup.txt", "Diff R↔S"),
        ("diff-running-vs-previous.txt", "Diff vs Prev"),
        ("meta.json", "Meta"),
    ]

    def __init__(self, master, device: Device):
        super().__init__(master)
        self.device = device
        self.title(f"کانفیگ — {device.name}")
        self.geometry("1100x720")
        self.minsize(900, 560)
        self.configure(fg_color=COLORS["panel"])
        self.transient(master)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            top,
            text=f"{device.name}  ·  {device.host}",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="e",
        ).pack(side="right", fill="x", expand=True)
        ctk.CTkButton(
            top,
            text="بکاپ الان",
            width=100,
            height=30,
            fg_color=COLORS["accent"],
            command=self._backup_now,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            top,
            text="باز کردن پوشه",
            width=110,
            height=30,
            fg_color=COLORS["panel2"],
            command=self._open_folder,
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            top,
            text="بستن",
            width=70,
            height=30,
            fg_color=COLORS["panel2"],
            command=self.destroy,
        ).pack(side="left")

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        left = ctk.CTkFrame(body, fg_color=COLORS["panel2"], width=260, corner_radius=10)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)
        ctk.CTkLabel(
            left, text="نسخه‌ها (زمانی)", font=ctk.CTkFont(size=13, weight="bold")
        ).pack(anchor="e", padx=10, pady=(10, 4))
        self.ver_list = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.ver_list.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        self._selected_version = ""

        right = ctk.CTkFrame(body, fg_color=COLORS["panel2"], corner_radius=10)
        right.pack(side="left", fill="both", expand=True)

        tools = ctk.CTkFrame(right, fg_color="transparent")
        tools.pack(fill="x", padx=10, pady=8)
        self.file_var = ctk.StringVar(value="running-config.cfg")
        self.file_menu = ctk.CTkOptionMenu(
            tools,
            values=[f"{lab} ({fn})" for fn, lab in self.FILES],
            command=self._on_file_menu,
            width=220,
        )
        self.file_menu.pack(side="right")
        self._file_label_to_name = {
            f"{lab} ({fn})": fn for fn, lab in self.FILES
        }

        ctk.CTkLabel(tools, text="مقایسه:", text_color=COLORS["muted"]).pack(
            side="left", padx=(0, 4)
        )
        self.cmp_a = ctk.CTkOptionMenu(tools, values=["—"], width=140)
        self.cmp_a.pack(side="left", padx=2)
        self.cmp_b = ctk.CTkOptionMenu(tools, values=["—"], width=140)
        self.cmp_b.pack(side="left", padx=2)
        ctk.CTkButton(
            tools,
            text="Diff Running",
            width=100,
            height=28,
            fg_color="#1f6f5b",
            command=lambda: self._run_compare("running"),
        ).pack(side="left", padx=4)
        ctk.CTkButton(
            tools,
            text="Diff Startup",
            width=100,
            height=28,
            fg_color="#1f6f5b",
            command=lambda: self._run_compare("startup"),
        ).pack(side="left", padx=2)

        self.viewer = ctk.CTkTextbox(
            right,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="none",
        )
        self.viewer.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.status = ctk.CTkLabel(
            self, text="", text_color=COLORS["muted"], anchor="w"
        )
        self.status.pack(fill="x", padx=14, pady=(0, 8))

        self._reload_versions()

    def _on_file_menu(self, choice: str) -> None:
        self.file_var.set(self._file_label_to_name.get(choice, "running-config.cfg"))
        self._show_selected()

    def _reload_versions(self) -> None:
        for child in self.ver_list.winfo_children():
            child.destroy()
        versions = list_versions(self.device.name, self.device.host)
        ids = [v.get("id", "") for v in versions if v.get("id")]
        if not ids:
            ctk.CTkLabel(
                self.ver_list,
                text="هنوز نسخه‌ای ذخیره نشده.\n«بکاپ الان» را بزنید.",
                text_color=COLORS["muted"],
                justify="center",
            ).pack(pady=20)
            self.cmp_a.configure(values=["—"])
            self.cmp_b.configure(values=["—"])
            self.viewer.delete("1.0", "end")
            self.status.configure(text="بدون نسخه")
            return
        self.cmp_a.configure(values=ids)
        self.cmp_b.configure(values=ids)
        self.cmp_a.set(ids[0])
        self.cmp_b.set(ids[min(1, len(ids) - 1)])
        self._selected_version = ids[0]
        for v in versions:
            vid = v.get("id", "")
            flags = []
            if v.get("startup_differs"):
                flags.append("R≠S")
            if v.get("running_changed"):
                flags.append("Δ")
            mark = " · ".join(flags)
            txt = f"{vid}" + (f"  [{mark}]" if mark else "")
            btn = ctk.CTkButton(
                self.ver_list,
                text=txt,
                anchor="e",
                height=30,
                fg_color=COLORS["panel"],
                command=lambda i=vid: self._select_version(i),
            )
            btn.pack(fill="x", pady=2)
        self._show_selected()

    def _select_version(self, version_id: str) -> None:
        self._selected_version = version_id
        self._show_selected()

    def _show_selected(self) -> None:
        if not self._selected_version:
            return
        fname = self.file_var.get() or "running-config.cfg"
        text = read_version_file(
            self.device.name, self.device.host, self._selected_version, fname
        )
        self.viewer.delete("1.0", "end")
        if not text.strip():
            self.viewer.insert(
                "1.0",
                f"(فایل {fname} در نسخه {self._selected_version} موجود نیست یا خالی است)",
            )
        else:
            self.viewer.insert("1.0", text)
        self.status.configure(
            text=f"نسخه {self._selected_version}  ·  {fname}  ·  {len(text)} chars"
        )

    def _run_compare(self, which: str) -> None:
        a = self.cmp_a.get()
        b = self.cmp_b.get()
        if not a or not b or a == "—" or b == "—":
            return
        diff = compare_versions(
            self.device.name, self.device.host, a, b, which=which
        )
        self.viewer.delete("1.0", "end")
        self.viewer.insert(
            "1.0",
            diff or "(هیچ اختلافی بین این دو نسخه نیست)",
        )
        self.status.configure(text=f"مقایسه {which}: {a} → {b}")

    def _open_folder(self) -> None:
        path = open_device_config_folder(self.device.name, self.device.host)
        try:
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo("پوشه", str(path), parent=self)

    def _backup_now(self) -> None:
        self.status.configure(text="در حال بکاپ از SSH…")
        self.update_idletasks()
        d = self.device

        def worker() -> None:
            result = backup_device_config(
                device_id=d.id,
                device_name=d.name,
                host=d.host,
                username=d.ssh_user,
                password=d.ssh_password,
                ssh_port=d.ssh_port or 22,
                enable_secret=d.ssh_enable,
                simulate=d.simulate,
            )
            self.after(0, lambda: self._backup_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _backup_done(self, result) -> None:
        if not result.ok:
            messagebox.showerror("بکاپ کانفیگ", result.error or "خطا", parent=self)
            self.status.configure(text=result.error or "خطا")
            return
        try:
            self.device.config_backed_up_at = time.time()
            master = self.master
            if hasattr(master, "app_data"):
                save_state(master.app_data)  # type: ignore[attr-defined]
        except Exception:
            pass
        notes = []
        if result.meta.get("skipped_identical"):
            notes.append("بدون تغییر نسبت به قبل (نسخه جدید ساخته نشد)")
        else:
            notes.append(f"نسخه {result.version_id}")
        if result.startup_differs:
            notes.append("Running ≠ Startup")
        if result.running_changed:
            notes.append("Running نسبت به قبل عوض شده")
        self.status.configure(text=" · ".join(notes))
        self._reload_versions()


class DetailDialog(ctk.CTkToplevel):
    """Device detail: faceplate first, then dashboard-style metric cards."""

    CARD = {
        "bg": "#f4f6f8",
        "card": "#ffffff",
        "head": "#eef1f4",
        "text": "#2c3e50",
        "muted": "#7f8c8d",
        "line": "#d5dbe3",
        "ok": "#27ae60",
        "warn": "#f1c40f",
        "crit": "#e74c3c",
        "cpu_user": "#8e44ad",
        "cpu_sys": "#27ae60",
        "cpu_idle": "#3498db",
        "poe_used": "#27ae60",
        "poe_free": "#f9e79f",
    }

    def __init__(self, master, device: Device, on_refresh_static=None):
        super().__init__(master)
        self.device = device
        self.on_refresh_static = on_refresh_static
        self._ports: list = []
        self._ifaces: list = []
        self._stack: list = []
        self._cdp: list = []
        self._ports_loading = False
        self._maximized = False
        self._restore_geom = "1280x820"
        self.title(f"جزئیات — {device.name}")
        self.minsize(1000, 640)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["panel"])
        self.transient(master)
        try:
            sw = max(1280, self.winfo_screenwidth())
            sh = max(700, self.winfo_screenheight())
            w = min(1480, max(1280, int(sw * 0.90)))
            h = min(900, max(760, int(sh * 0.86)))
            self._restore_geom = f"{w}x{h}"
            self.geometry(self._restore_geom)
        except Exception:
            self.geometry("1280x820")

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 6))

        win_btns = ctk.CTkFrame(header, fg_color="transparent")
        win_btns.pack(side="left")
        ctk.CTkButton(
            win_btns,
            text="─",
            width=36,
            height=28,
            fg_color=COLORS["panel2"],
            command=self._minimize,
        ).pack(side="left", padx=(0, 4))
        self.max_btn = ctk.CTkButton(
            win_btns,
            text="□",
            width=36,
            height=28,
            fg_color=COLORS["panel2"],
            command=self._toggle_maximize,
        )
        self.max_btn.pack(side="left")

        badge_txt, badge_color = TYPE_BADGE.get(
            device.device_type, TYPE_BADGE["unknown"]
        )
        ctk.CTkLabel(
            header,
            text=badge_txt,
            width=42,
            height=42,
            corner_radius=10,
            fg_color=badge_color,
            text_color="#041018",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="right", padx=(8, 0))

        titles = ctk.CTkFrame(header, fg_color="transparent")
        titles.pack(side="right", fill="x", expand=True)
        ctk.CTkLabel(
            titles,
            text=device.name,
            font=ctk.CTkFont(size=17, weight="bold"),
            anchor="e",
        ).pack(fill="x")
        type_fa = TYPE_LABELS.get(device.device_type, "نامشخص")
        ctk.CTkLabel(
            titles,
            text=f"{type_fa}  ·  {device.host}",
            text_color=COLORS["muted"],
            anchor="e",
            font=ctk.CTkFont(size=12),
        ).pack(fill="x")

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkButton(
            actions,
            text="بروزرسانی اطلاعات ثابت",
            width=170,
            height=30,
            fg_color=COLORS["accent"],
            command=self._refresh_static,
        ).pack(side="right")
        self.ports_btn = ctk.CTkButton(
            actions,
            text="خواندن پورت‌ها",
            width=120,
            height=30,
            fg_color="#1f6f5b",
            hover_color="#26856d",
            command=self._load_ports,
        )
        self.ports_btn.pack(side="right", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="بستن",
            width=80,
            height=30,
            fg_color=COLORS["panel2"],
            command=self.destroy,
        ).pack(side="left")

        self.body = ctk.CTkScrollableFrame(
            self, fg_color=self.CARD["bg"], corner_radius=10
        )
        self.body.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self._fitted_once = False
        self._render()
        self.after(200, self._load_ports)
        self.after(300, self._tune_detail_scroll)
        self.after(400, self._fit_to_content)

    def _minimize(self) -> None:
        try:
            self.iconify()
        except Exception:
            pass

    def _toggle_maximize(self) -> None:
        try:
            if self._maximized:
                self.state("normal")
                self.geometry(self._restore_geom)
                self._maximized = False
                self.max_btn.configure(text="□")
            else:
                self._restore_geom = self.geometry()
                self.state("zoomed")
                self._maximized = True
                self.max_btn.configure(text="❐")
        except Exception:
            if self._maximized:
                self.geometry(self._restore_geom)
                self._maximized = False
                self.max_btn.configure(text="□")
            else:
                self._restore_geom = self.geometry()
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
                self.geometry(f"{sw}x{sh - 40}+0+0")
                self._maximized = True
                self.max_btn.configure(text="❐")

    def _fit_to_content(self) -> None:
        if getattr(self, "_maximized", False):
            return
        if getattr(self, "_fitted_once", False):
            return
        try:
            self.update_idletasks()
            sw = max(1280, self.winfo_screenwidth())
            sh = max(700, self.winfo_screenheight())
            w = min(1480, max(1280, int(sw * 0.90)))
            h = min(900, max(760, int(sh * 0.86)))
            self._restore_geom = f"{w}x{h}"
            self.geometry(self._restore_geom)
            if self.master and self.master.winfo_ismapped():
                x = self.master.winfo_rootx() + (
                    self.master.winfo_width() - w
                ) // 2
                y = max(10, self.master.winfo_rooty() + 10)
                self.geometry(f"{w}x{h}+{max(0, x)}+{y}")
            self._fitted_once = True
        except Exception:
            pass

    def _tune_detail_scroll(self) -> None:
        """Faster mouse-wheel steps; avoid bind_all so other windows stay intact."""
        try:
            canvas = self.body._parent_canvas  # type: ignore[attr-defined]
        except Exception:
            return

        def _wheel(event) -> str:
            delta = getattr(event, "delta", 0) or 0
            if delta:
                # 3 units per notch → fewer redraws than 1-unit micro-scrolls
                steps = int(-1 * (delta / 120) * 3) or (-3 if delta > 0 else 3)
                canvas.yview_scroll(steps, "units")
            elif getattr(event, "num", None) == 4:
                canvas.yview_scroll(-3, "units")
            elif getattr(event, "num", None) == 5:
                canvas.yview_scroll(3, "units")
            return "break"

        # Replace default 1-unit wheel handler with larger steps
        canvas.bind("<MouseWheel>", _wheel)
        canvas.bind("<Button-4>", _wheel)
        canvas.bind("<Button-5>", _wheel)
        try:
            self.body.bind("<MouseWheel>", _wheel)
        except Exception:
            pass

    def _dash_card(
        self,
        parent,
        title: str,
        icon: str,
        meta: str = "",
        *,
        card_key: str = "",
    ) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=self.CARD["card"],
            corner_radius=10,
            border_width=1,
            border_color=self.CARD["line"],
        )
        head = ctk.CTkFrame(card, fg_color=self.CARD["head"], corner_radius=8)
        head.pack(fill="x", padx=6, pady=6)
        ctk.CTkLabel(
            head,
            text=f"{icon}  {title}",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=self.CARD["text"],
            anchor="w",
        ).pack(side="left", padx=8, pady=6)
        right = ctk.CTkFrame(head, fg_color="transparent")
        right.pack(side="right", padx=4)
        if card_key:
            ctk.CTkButton(
                right,
                text="▲",
                width=28,
                height=24,
                fg_color="#d5dbe3",
                text_color=self.CARD["text"],
                hover_color="#c0c7d0",
                command=lambda k=card_key: self._move_detail_card(k, -1),
            ).pack(side="left", padx=2)
            ctk.CTkButton(
                right,
                text="▼",
                width=28,
                height=24,
                fg_color="#d5dbe3",
                text_color=self.CARD["text"],
                hover_color="#c0c7d0",
                command=lambda k=card_key: self._move_detail_card(k, 1),
            ).pack(side="left", padx=2)
        if meta:
            ctk.CTkLabel(
                right,
                text=meta,
                font=ctk.CTkFont(size=10),
                text_color=self.CARD["muted"],
                anchor="e",
            ).pack(side="left", padx=6)
        return card

    def _app_state(self) -> Optional[AppState]:
        master = self.master
        return getattr(master, "app_data", None)

    def _card_order(self) -> list[str]:
        state = self._app_state()
        if state is None:
            return list(DEFAULT_DETAIL_CARD_ORDER)
        return state.normalized_detail_order()

    def _move_detail_card(self, card_key: str, delta: int) -> None:
        state = self._app_state()
        if state is None or card_key not in DETAIL_CARD_LABELS:
            return
        order = state.normalized_detail_order()
        try:
            idx = order.index(card_key)
        except ValueError:
            return
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(order):
            return
        order[idx], order[new_idx] = order[new_idx], order[idx]
        state.detail_card_order = order
        try:
            save_state(state)
        except Exception:
            pass
        self._render()

    def _port_num(self, label: str) -> Optional[int]:
        try:
            return int(str(label).split("/")[-1])
        except (TypeError, ValueError):
            return None

    def _port_face_color(self, status: str) -> str:
        return PORT_COLORS.get(status, PORT_COLORS["disabled"])

    def _draw_rj45_jack(
        self,
        cv: Canvas,
        x: int,
        y: int,
        port: dict,
        *,
        number_on_top: bool,
        cell_w: int = 28,
        cell_h: int = 38,
    ) -> None:
        """Paint one RJ45 cell onto a Canvas (no child widgets)."""
        label = str(port.get("label", "?"))
        num = self._port_num(label)
        num_txt = f"{num:02d}" if num is not None else label[-2:]
        status = port.get("status") or "disabled"
        color = self._port_face_color(status)
        speed = (port.get("speed") or "").strip() or ("·" if status == "up" else "")
        jack_w, jack_h = 24, 20
        jack_x = x + (cell_w - jack_w) // 2
        if number_on_top:
            num_y = y + 6
            jack_y = y + 12
        else:
            jack_y = y + 2
            num_y = y + cell_h - 6
        cv.create_text(
            x + cell_w // 2,
            num_y,
            text=num_txt,
            fill=PORT_PANEL["text"],
            font=("Segoe UI", 8),
            anchor="center",
        )
        cv.create_rectangle(
            jack_x,
            jack_y,
            jack_x + jack_w,
            jack_y + jack_h,
            fill=color,
            outline="#5f6b75",
            width=1,
        )
        # top LED notch
        cv.create_rectangle(
            jack_x + 7,
            jack_y + 2,
            jack_x + 17,
            jack_y + 5,
            fill="#ffffff",
            outline="",
        )
        cv.create_text(
            jack_x + jack_w // 2,
            jack_y + 13,
            text=speed,
            fill="#ffffff" if status == "up" else "#e8edf2",
            font=("Segoe UI", 7, "bold"),
            anchor="center",
        )

    def _paint_port_grid_canvas(
        self, parent, by_num: dict[int, dict], bg: str = "#c2d0db"
    ) -> None:
        if not by_num:
            ctk.CTkLabel(
                parent,
                text="پورتی برای این عضو استک نیست",
                text_color=PORT_PANEL["muted"],
            ).pack(anchor="w", pady=4)
            return
        max_n = max(by_num)
        groups: list[list[int]] = []
        start = 1
        while start <= max_n:
            groups.append(list(range(start, start + 12)))
            start += 12
        cell_w, cell_h, gap, group_gap = 28, 38, 2, 8
        width = (
            len(groups) * (12 * (cell_w + gap))
            + max(0, len(groups) - 1) * group_gap
            + 4
        )
        height = cell_h * 2 + 4
        cv = Canvas(
            parent,
            width=max(40, width),
            height=height,
            bg=bg,
            highlightthickness=0,
            bd=0,
        )
        cv.pack(anchor="nw")
        for gi, nums in enumerate(groups):
            ox = gi * (12 * (cell_w + gap) + group_gap)
            for n in nums:
                # odd on top row, even on bottom — matching physical layout
                if n % 2 == 1:
                    # odd ports: index among odds in this group
                    odds = [x for x in nums if x % 2 == 1]
                    idx = odds.index(n) if n in odds else 0
                    x = ox + idx * (cell_w + gap)
                    y = 0
                    if n in by_num:
                        self._draw_rj45_jack(
                            cv, x, y, by_num[n], number_on_top=True
                        )
                else:
                    evens = [x for x in nums if x % 2 == 0]
                    idx = evens.index(n) if n in evens else 0
                    x = ox + idx * (cell_w + gap)
                    y = cell_h
                    if n in by_num:
                        self._draw_rj45_jack(
                            cv, x, y, by_num[n], number_on_top=False
                        )

    def _paint_uplink_canvas(
        self, parent, items: list[dict], title: str = "Uplink"
    ) -> None:
        box = ctk.CTkFrame(
            parent,
            fg_color="#b7c6d4",
            corner_radius=8,
            border_width=1,
            border_color="#7f8c9a",
        )
        box.pack(side="left", padx=(10, 0), anchor="n")
        ctk.CTkLabel(
            box,
            text=title,
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=PORT_PANEL["text"],
        ).pack(padx=6, pady=(4, 2))
        cell_w, gap = 28, 4
        width = max(40, len(items) * (cell_w + gap) + 8)
        cv = Canvas(
            box,
            width=width,
            height=42,
            bg="#b7c6d4",
            highlightthickness=0,
            bd=0,
        )
        cv.pack(padx=4, pady=(0, 6))
        for i, p in enumerate(items):
            self._draw_rj45_jack(
                cv, 4 + i * (cell_w + gap), 2, p, number_on_top=True
            )

    def _ensure_detail_tree_style(self) -> None:
        if getattr(DetailDialog, "_tree_style_ready", False):
            return
        try:
            style = ttk.Style()
            try:
                style.theme_use("clam")
            except Exception:
                pass
            style.configure(
                "Detail.Treeview",
                background="#ffffff",
                fieldbackground="#ffffff",
                foreground="#1b2838",
                rowheight=22,
                font=("Segoe UI", 9),
                borderwidth=0,
            )
            style.configure(
                "Detail.Treeview.Heading",
                background="#e8edf3",
                foreground="#1b2838",
                font=("Segoe UI", 9, "bold"),
                relief="flat",
            )
            style.map(
                "Detail.Treeview",
                background=[("selected", "#c5d8ef")],
                foreground=[("selected", "#1b2838")],
            )
            DetailDialog._tree_style_ready = True
        except Exception:
            pass

    def _make_detail_tree(
        self,
        parent,
        columns: list[tuple[str, str, int]],
        rows: list[tuple],
        *,
        height: int = 14,
        status_col: Optional[int] = None,
    ) -> None:
        """Lightweight native table — scrolls internally, few Tk widgets."""
        self._ensure_detail_tree_style()
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        ids = [c[0] for c in columns]
        tree = ttk.Treeview(
            wrap,
            columns=ids,
            show="headings",
            height=min(height, max(4, len(rows) or 4)),
            style="Detail.Treeview",
            selectmode="browse",
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for key, title, width in columns:
            tree.heading(key, text=title, anchor="w")
            tree.column(key, width=width, minwidth=40, anchor="w", stretch=True)
        tree.tag_configure("up", foreground="#00a844")
        tree.tag_configure("down", foreground="#6b7680")
        tree.tag_configure("disabled", foreground="#8b97a3")
        tree.tag_configure("odd", background="#f8fafc")
        tree.tag_configure("even", background="#ffffff")
        for i, row in enumerate(rows):
            tags = ["odd" if i % 2 else "even"]
            if status_col is not None and status_col < len(row):
                st = str(row[status_col]).lower()
                if "up" in st:
                    tags.append("up")
                elif "dis" in st:
                    tags.append("disabled")
                elif "down" in st:
                    tags.append("down")
            tree.insert("", "end", values=row, tags=tuple(tags))

        # Keep mouse-wheel on the tree from fighting the outer scroll too hard:
        # when tree can scroll, consume the event; otherwise bubble to parent.
        def _tree_wheel(event):
            try:
                first, last = tree.yview()
            except Exception:
                return
            delta = getattr(event, "delta", 0) or 0
            going_up = delta > 0 or getattr(event, "num", None) == 4
            going_down = delta < 0 or getattr(event, "num", None) == 5
            if going_up and first <= 0.0:
                return
            if going_down and last >= 1.0:
                return
            if delta:
                tree.yview_scroll(int(-1 * (delta / 120)), "units")
            elif going_up:
                tree.yview_scroll(-1, "units")
            elif going_down:
                tree.yview_scroll(1, "units")
            return "break"

        tree.bind("<MouseWheel>", _tree_wheel)
        tree.bind("<Button-4>", _tree_wheel)
        tree.bind("<Button-5>", _tree_wheel)

    def _build_switch_faceplate(self, parent, ports: list[dict]) -> None:
        face = ctk.CTkFrame(parent, fg_color=PORT_PANEL["bg"], corner_radius=10)
        face.pack(fill="x", padx=8, pady=(2, 8))

        # Avatar / Active on TOP so port blocks stay close
        badge_txt, badge_color = TYPE_BADGE.get(
            self.device.device_type, TYPE_BADGE["unknown"]
        )
        topbar = ctk.CTkFrame(face, fg_color="transparent")
        topbar.pack(fill="x", padx=10, pady=(8, 2))
        avatar = ctk.CTkFrame(
            topbar,
            width=40,
            height=40,
            corner_radius=20,
            fg_color=badge_color if self.device.device_type != "unknown" else PORT_PANEL["active"],
        )
        avatar.pack(side="left")
        avatar.pack_propagate(False)
        ctk.CTkLabel(
            avatar,
            text=badge_txt,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#041018",
        ).place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(
            topbar,
            text="Active",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=PORT_PANEL["text"],
        ).pack(side="left", padx=8)
        type_fa = TYPE_LABELS.get(self.device.device_type, "دستگاه")
        ctk.CTkLabel(
            topbar,
            text=f"{type_fa}  ·  {self.device.name}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=PORT_PANEL["text"],
        ).pack(side="right")

        # Port grid only (no side column — avoids stretching odd/even row gap)
        main = ctk.CTkFrame(face, fg_color="transparent")
        main.pack(fill="x", padx=8, pady=(2, 2))

        access_ports = [p for p in ports if (p.get("kind") or "access") == "access"]
        uplink = [p for p in ports if p.get("kind") == "uplink"]
        tunnels = [p for p in ports if p.get("kind") == "tunnel"]
        portchannels = [p for p in ports if p.get("kind") == "portchannel"]

        # Group access ports by stack member (1, 2, …)
        # 24-port switches: ports 25+ are SFP uplinks → separate box
        by_stack: dict[int, dict[int, dict]] = {}
        for p in access_ports:
            stack = int(p.get("stack") or 1)
            n = int(p.get("port") or 0) or self._port_num(str(p.get("label", ""))) or 0
            if not n:
                continue
            by_stack.setdefault(stack, {})[n] = p

        stack_uplinks: dict[int, list[dict]] = {}
        uplink_titles: dict[int, str] = {}
        for stack_id, nums in list(by_stack.items()):
            max_n = max(nums) if nums else 0
            limit = None
            # 48-port: ports 49+ are uplinks
            if any(n >= 49 for n in nums):
                limit = 48
            # 24-port: ports 25+ are uplinks (SFP usually ≤32)
            elif any(n >= 25 for n in nums) and max_n <= 32:
                limit = 24
            if limit is None:
                continue
            keep = {n: p for n, p in nums.items() if n <= limit}
            extra = [p for n, p in nums.items() if n > limit]
            by_stack[stack_id] = keep
            if extra:
                stack_uplinks[stack_id] = sorted(
                    extra, key=lambda x: int(x.get("port") or 0)
                )
                uplink_titles[stack_id] = f"Uplink ({limit + 1}+)"

        stack_meta = {int(m.get("num") or 0): m for m in (self._stack or [])}

        if by_stack:
            for stack_id in sorted(by_stack):
                meta_s = stack_meta.get(stack_id) or {}
                role = meta_s.get("role") or ""
                is_master = bool(meta_s.get("is_master"))
                role_txt = ""
                if role:
                    role_txt = "  ·  MASTER" if is_master else f"  ·  {role.upper()}"
                member_box = ctk.CTkFrame(main, fg_color="#c2d0db", corner_radius=8)
                member_box.pack(fill="x", pady=(0, 6), anchor="w")
                ctk.CTkLabel(
                    member_box,
                    text=f"Switch {stack_id}{role_txt}"
                    + (f"  ·  {meta_s.get('mac')}" if meta_s.get("mac") else ""),
                    font=ctk.CTkFont(size=11, weight="bold"),
                    text_color=PORT_PANEL["text"],
                    anchor="w",
                ).pack(fill="x", padx=8, pady=(4, 2))
                grid_wrap = ctk.CTkFrame(member_box, fg_color="transparent")
                grid_wrap.pack(fill="x", padx=6, pady=(0, 6))
                left = ctk.CTkFrame(grid_wrap, fg_color="#c2d0db")
                left.pack(side="left", anchor="n")
                self._paint_port_grid_canvas(left, by_stack[stack_id], bg="#c2d0db")
                if stack_uplinks.get(stack_id):
                    self._paint_uplink_canvas(
                        grid_wrap,
                        stack_uplinks[stack_id],
                        title=uplink_titles.get(stack_id, "Uplink"),
                    )
        else:
            ctk.CTkLabel(
                main,
                text="پورت فیزیکی استاندارد یافت نشد",
                text_color=PORT_PANEL["muted"],
            ).pack(pady=8, anchor="w")

        # Stack summary when SNMP stack table exists but ports had no stack field
        if self._stack:
            srow = ctk.CTkFrame(face, fg_color="transparent")
            srow.pack(fill="x", padx=10, pady=(2, 2))
            ctk.CTkLabel(
                srow,
                text="Stack:",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color=PORT_PANEL["muted"],
            ).pack(side="left")
            for m in self._stack:
                tag = "MASTER" if m.get("is_master") else (m.get("role") or "member").upper()
                color = "#27ae60" if m.get("is_master") else "#5dade2"
                ctk.CTkLabel(
                    srow,
                    text=f"SW{m.get('num')} {tag}",
                    font=ctk.CTkFont(size=10, weight="bold"),
                    text_color="#041018",
                    fg_color=color,
                    corner_radius=4,
                    height=20,
                    padx=8,
                ).pack(side="left", padx=3)

        # Uplink / Port-channel / Tunnels BELOW ports
        extra = ctk.CTkFrame(face, fg_color="transparent")
        extra.pack(fill="x", padx=10, pady=(4, 2))

        def _chip_row(title: str, items: list[dict]) -> None:
            row = ctk.CTkFrame(extra, fg_color="transparent")
            row.pack(fill="x", pady=1, anchor="w")
            ctk.CTkLabel(
                row,
                text=f"{title}:",
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color=PORT_PANEL["muted"],
                width=90,
                anchor="w",
            ).pack(side="left")
            if not items:
                ctk.CTkLabel(
                    row,
                    text="—",
                    font=ctk.CTkFont(size=10),
                    text_color=PORT_PANEL["muted"],
                ).pack(side="left")
                return
            # Compact text chips on one canvas row when many
            if len(items) > 8:
                chip_cv = Canvas(
                    row,
                    height=22,
                    bg=PORT_PANEL["bg"],
                    highlightthickness=0,
                    bd=0,
                )
                chip_cv.pack(side="left", fill="x", expand=True)
                x = 2
                for p in items[:40]:
                    color = self._port_face_color(p.get("status") or "disabled")
                    stack = p.get("stack")
                    prefix = f"S{stack}/" if stack else ""
                    txt = f"{prefix}{p.get('label')} {p.get('speed') or ''}".strip()
                    tw = max(36, len(txt) * 7)
                    chip_cv.create_rectangle(
                        x, 2, x + tw, 20, fill=color, outline="", width=0
                    )
                    chip_cv.create_text(
                        x + tw // 2,
                        11,
                        text=txt,
                        fill="#041018",
                        font=("Segoe UI", 8, "bold"),
                        anchor="center",
                    )
                    x += tw + 4
                chip_cv.configure(width=min(900, x + 4))
                return
            for p in items[:24]:
                color = self._port_face_color(p.get("status") or "disabled")
                stack = p.get("stack")
                prefix = f"S{stack}/" if stack else ""
                ctk.CTkLabel(
                    row,
                    text=f"{prefix}{p.get('label')} {p.get('speed') or ''}".strip(),
                    font=ctk.CTkFont(size=9, weight="bold"),
                    text_color="#041018",
                    fg_color=color,
                    corner_radius=3,
                    height=18,
                    padx=6,
                ).pack(side="left", padx=2)

        if uplink:
            _chip_row("Uplink", uplink)
        if portchannels:
            _chip_row("Port-channel", portchannels)
        if tunnels:
            _chip_row("Tunnels", tunnels)

        foot = ctk.CTkFrame(face, fg_color="transparent")
        foot.pack(fill="x", padx=10, pady=(0, 8))
        mac = (self.device.static_info or {}).get("serial") or "—"
        ctk.CTkLabel(
            foot,
            text=f"Host: {self.device.name}   Serial: {mac}",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=PORT_PANEL["text"],
        ).pack(anchor="e")

    def _render_ports_section(self) -> None:
        meta = ""
        if self._ports_loading:
            meta = "در حال خواندن…"
        elif self._ports:
            up_n = sum(1 for p in self._ports if p.get("status") == "up")
            down_n = sum(1 for p in self._ports if p.get("status") == "down")
            dis_n = sum(1 for p in self._ports if p.get("status") == "disabled")
            meta = f"Up:{up_n}  Down:{down_n}  Dis:{dis_n}"
        else:
            meta = "خوانده نشده"

        box = self._dash_card(
            self.body,
            "نمای فیزیکی پورت‌ها",
            "[P]",
            meta=meta,
            card_key="ports",
        )
        box.pack(fill="x", pady=(4, 8), padx=2)

        if self._ports_loading:
            ctk.CTkLabel(
                box,
                text="صبر کنید — فقط همین دستگاه از SNMP خوانده می‌شود",
                text_color=self.CARD["muted"],
                anchor="e",
            ).pack(fill="x", padx=12, pady=(0, 12))
            return

        if not self._ports:
            ctk.CTkLabel(
                box,
                text="روی «خواندن پورت‌ها» بزنید",
                text_color=self.CARD["muted"],
                anchor="e",
            ).pack(fill="x", padx=12, pady=(0, 12))
            return

        self._build_switch_faceplate(box, self._ports)

    def _draw_thermometer(self, parent, temp: Optional[float]) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(expand=True, fill="both", padx=8, pady=4)
        cv = Canvas(wrap, width=90, height=180, bg=self.CARD["card"], highlightthickness=0)
        cv.pack()
        # scale tube
        x0, y0, x1, y1 = 38, 18, 58, 145
        cv.create_rectangle(x0, y0, x1, y1, outline="#95a5a6", width=2)
        # color bands (bottom green → mid yellow → top red)
        bands = [
            (100, 60, "#e74c3c"),
            (60, 40, "#f1c40f"),
            (40, 0, "#27ae60"),
        ]
        for hi, lo, col in bands:
            ya = y1 - (hi / 100.0) * (y1 - y0)
            yb = y1 - (lo / 100.0) * (y1 - y0)
            cv.create_rectangle(x0 + 2, ya, x1 - 2, yb, fill=col, outline="")
        # mercury fill
        t = 0.0 if temp is None else max(0.0, min(100.0, float(temp)))
        fill_y = y1 - (t / 100.0) * (y1 - y0)
        cv.create_oval(32, 138, 64, 170, fill="#2c3e50", outline="#2c3e50")
        cv.create_rectangle(x0 + 4, fill_y, x1 - 4, y1, fill="#2c3e50", outline="")
        for mark in (0, 20, 40, 60, 80, 100):
            yy = y1 - (mark / 100.0) * (y1 - y0)
            cv.create_text(28, yy, text=str(mark), fill="#7f8c8d", font=("Segoe UI", 8), anchor="e")
        label = "—" if temp is None else f"{temp:.0f}°C"
        ctk.CTkLabel(
            wrap,
            text=f"System Temperature : {label}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.CARD["text"],
        ).pack(pady=(4, 8))

    def _draw_poe_pie(self, parent, used_pct: Optional[float], capacity_w) -> None:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(expand=True, fill="both", padx=8, pady=4)
        cv = Canvas(wrap, width=160, height=140, bg=self.CARD["card"], highlightthickness=0)
        cv.pack()
        pct = 0.0 if used_pct is None else max(0.0, min(100.0, float(used_pct)))
        extent = pct / 100.0 * 360.0
        cv.create_oval(20, 10, 140, 130, fill=self.CARD["poe_free"], outline="")
        if extent > 0:
            cv.create_arc(
                20,
                10,
                140,
                130,
                start=90,
                extent=-extent,
                fill=self.CARD["poe_used"],
                outline="",
            )
        cv.create_oval(55, 45, 105, 95, fill=self.CARD["card"], outline="")
        legend = ctk.CTkFrame(wrap, fg_color="transparent")
        legend.pack()
        ctk.CTkLabel(
            legend,
            text="● Unused",
            text_color="#b7950b",
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=8)
        ctk.CTkLabel(
            legend,
            text="● PoE",
            text_color=self.CARD["poe_used"],
            font=ctk.CTkFont(size=11),
        ).pack(side="left", padx=8)
        cap = "—" if capacity_w is None else f"{capacity_w:.0f} W"
        used = "—" if used_pct is None else f"{used_pct:.0f}%"
        ctk.CTkLabel(
            wrap,
            text=f"Total Power Supported : {cap}   ·   Used {used}",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.CARD["text"],
        ).pack(pady=(6, 8))

    def _render_cpu_mem_card(self) -> None:
        d = self.device
        dyn = d.dynamic_info or {}
        card = self._dash_card(
            self.body,
            "CPU & Memory Pressure",
            "CPU",
            meta=format_ts(d.dynamic_fetched_at),
            card_key="cpu_mem",
        )
        card.pack(fill="x", pady=6, padx=2)
        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=10, pady=(0, 10))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        # CPU column
        cpu_box = ctk.CTkFrame(body, fg_color="transparent")
        cpu_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(
            cpu_box,
            text="CPU Utilization",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.CARD["text"],
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        cpu = dyn.get("cpu_percent")
        if cpu is None:
            user, system, idle = 0.0, 0.0, 100.0
        else:
            user = float(cpu) * 0.65
            system = float(cpu) * 0.35
            idle = max(0.0, 100.0 - float(cpu))
        for name, val, col in (
            ("User", user, self.CARD["cpu_user"]),
            ("System", system, self.CARD["cpu_sys"]),
            ("Idle", idle, self.CARD["cpu_idle"]),
        ):
            row = ctk.CTkFrame(cpu_box, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row, text=name, width=60, anchor="w", text_color=self.CARD["muted"]
            ).pack(side="left")
            bar = ctk.CTkProgressBar(row, width=140, height=10, progress_color=col)
            bar.pack(side="left", padx=6)
            bar.set(min(1.0, val / 100.0))
            ctk.CTkLabel(
                row,
                text=f"{val:.1f}%",
                width=50,
                anchor="e",
                text_color=self.CARD["text"],
                font=ctk.CTkFont(size=11, weight="bold"),
            ).pack(side="left")
        total = "—" if cpu is None else f"{float(cpu):.1f}%"
        ctk.CTkLabel(
            cpu_box,
            text=f"Total CPU : {total}",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=self.CARD["text"],
            anchor="w",
        ).pack(fill="x", pady=(8, 0))

        # Memory column
        mem_box = ctk.CTkFrame(body, fg_color="transparent")
        mem_box.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(
            mem_box,
            text="Memory Utilization",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.CARD["text"],
            anchor="w",
        ).pack(fill="x", pady=(0, 4))
        used = dyn.get("memory_used")
        free = dyn.get("memory_free")
        mpct = dyn.get("memory_percent")
        total_m = None
        if used is not None and free is not None:
            try:
                total_m = int(used) + int(free)
            except Exception:
                total_m = None
        for label, val in (
            ("Total", total_m),
            ("Used", used),
            ("Free", free),
            ("Used %", None if mpct is None else f"{mpct}%"),
        ):
            row = ctk.CTkFrame(mem_box, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                row, text=label, width=70, anchor="w", text_color=self.CARD["muted"]
            ).pack(side="left")
            ctk.CTkLabel(
                row,
                text="—" if val is None else str(val),
                anchor="e",
                text_color=self.CARD["text"],
                font=ctk.CTkFont(size=11, weight="bold"),
            ).pack(side="right")
        gauge = ctk.CTkProgressBar(
            mem_box, height=14, progress_color=self.CARD["ok"]
        )
        gauge.pack(fill="x", pady=(10, 2))
        gval = 0.0 if mpct is None else min(1.0, float(mpct) / 100.0)
        if mpct is not None and float(mpct) >= 95:
            gauge.configure(progress_color=self.CARD["crit"])
        elif mpct is not None and float(mpct) >= 80:
            gauge.configure(progress_color=self.CARD["warn"])
        gauge.set(gval)
        ctk.CTkLabel(
            mem_box,
            text="Healthy <80%   ·   Critical ≥95%",
            font=ctk.CTkFont(size=10),
            text_color=self.CARD["muted"],
            anchor="w",
        ).pack(fill="x")

    def _info_line(self, parent, label: str, value) -> None:
        if value is None or value == "":
            return
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=1)
        ctk.CTkLabel(
            r, text="●", width=14, text_color=self.CARD["muted"]
        ).pack(side="left")
        ctk.CTkLabel(
            r,
            text=label,
            width=100,
            anchor="w",
            text_color=self.CARD["muted"],
            font=ctk.CTkFont(size=10),
        ).pack(side="left")
        ctk.CTkLabel(
            r,
            text=str(value),
            anchor="w",
            text_color=self.CARD["text"],
            font=ctk.CTkFont(size=10, weight="bold"),
            wraplength=220,
        ).pack(side="left", fill="x", expand=True)

    def _render_bottom_cards(self) -> None:
        d = self.device
        dyn = d.dynamic_info or {}
        st = d.static_info or {}
        wrap = self._dash_card(
            self.body,
            "دما / سیستم / PoE",
            "Dash",
            card_key="dashboard",
        )
        wrap.pack(fill="x", pady=4, padx=2)
        row = ctk.CTkFrame(wrap, fg_color="transparent")
        row.pack(fill="x", padx=6, pady=(0, 8))
        row.grid_columnconfigure((0, 1, 2), weight=1)

        # Temperature
        tcard = self._dash_card(row, "Temperature", "Temp", meta=format_ts(d.dynamic_fetched_at))
        tcard.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._draw_thermometer(tcard, d.temperature)

        # System Information (richer)
        scard = self._dash_card(
            row, "System Information", "Sys", meta=format_ts(d.static_fetched_at)
        )
        scard.grid(row=0, column=1, sticky="nsew", padx=4)
        info = ctk.CTkFrame(scard, fg_color="transparent")
        info.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for label, value in (
            ("Hostname", st.get("sys_name") or d.name),
            ("IP / Host", d.host),
            ("Type", st.get("device_type_label") or TYPE_LABELS.get(d.device_type, "—")),
            ("Model", st.get("model")),
            ("Serial", st.get("serial")),
            ("IOS / SW", st.get("ios_version")),
            ("Firmware", st.get("firmware_rev")),
            ("HW Rev", st.get("hardware_rev")),
            ("Entity", st.get("entity_name")),
            ("Uptime", dyn.get("uptime")),
            ("Interfaces", st.get("if_number") or dyn.get("if_number")),
            ("Location", st.get("sys_location")),
            ("Contact", st.get("sys_contact")),
            ("Status", status_text(d)),
        ):
            self._info_line(info, label, value)

        # PoE
        pcard = self._dash_card(
            row, "PoE Power Consumption", "PoE", meta=format_ts(d.dynamic_fetched_at)
        )
        pcard.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        self._draw_poe_pie(
            pcard, dyn.get("poe_percent"), dyn.get("poe_capacity_w")
        )

    def _render_full_inventory(self) -> None:
        """Dump every available static + dynamic field."""
        d = self.device
        st = dict(d.static_info or {})
        if "community" not in st:
            st["community"] = d.community
        if "snmp_port" not in st:
            st["snmp_port"] = d.port
        rows = static_rows(st) + dynamic_rows(d.dynamic_info or {}, d.temperature)
        if not rows and not d.last_error:
            return
        card = self._dash_card(
            self.body,
            "Full Device Inventory",
            "All",
            meta=f"static {format_ts(d.static_fetched_at)} · dynamic {format_ts(d.dynamic_fetched_at)}",
            card_key="inventory",
        )
        card.pack(fill="x", pady=6, padx=2)
        tree_rows = [(str(label), str(value)) for label, value in rows]
        self._make_detail_tree(
            card,
            [("field", "Field", 220), ("value", "Value", 700)],
            tree_rows,
            height=min(16, max(6, len(tree_rows))),
        )

    def _render_iface_table(self) -> None:
        """Full interface list: name, description, status, speed, traffic."""
        card = self._dash_card(
            self.body,
            "Interface Inventory",
            "IF",
            meta=f"{len(self._ifaces)} interfaces" if self._ifaces else "",
            card_key="iface",
        )
        card.pack(fill="x", pady=6, padx=2)

        if self._ports_loading and not self._ifaces:
            ctk.CTkLabel(
                card,
                text="در حال خواندن اینترفیس‌ها…",
                text_color=self.CARD["muted"],
                anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 10))
            return

        if not self._ifaces:
            ctk.CTkLabel(
                card,
                text="لیست اینترفیس هنوز خوانده نشده",
                text_color=self.CARD["muted"],
                anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 10))
            return

        up_n = sum(1 for i in self._ifaces if i.get("status") == "up")
        down_n = sum(1 for i in self._ifaces if i.get("status") == "down")
        dis_n = sum(1 for i in self._ifaces if i.get("status") == "disabled")
        ctk.CTkLabel(
            card,
            text=f"Up:{up_n}   Down:{down_n}   Disabled:{dis_n}",
            text_color=self.CARD["muted"],
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", padx=12, pady=(0, 4))

        tree_rows = []
        for iface in self._ifaces:
            st = (iface.get("status") or "disabled").upper()
            tree_rows.append(
                (
                    str(iface.get("name") or "—")[:80],
                    str(iface.get("description") or "—")[:80],
                    str(iface.get("ip") or "—")[:40],
                    st,
                    str(iface.get("speed") or "—")[:20],
                    str(iface.get("in_fmt") or "—")[:20],
                    str(iface.get("out_fmt") or "—")[:20],
                )
            )
        self._make_detail_tree(
            card,
            [
                ("name", "Interface", 170),
                ("desc", "Description", 220),
                ("ip", "IP", 120),
                ("status", "Status", 70),
                ("speed", "Speed", 60),
                ("inn", "In", 80),
                ("out", "Out", 80),
            ],
            tree_rows,
            height=14,
            status_col=3,
        )

    def _render_cdp_card(self) -> None:
        src = ""
        if self._cdp:
            kinds = {n.get("source") or "CDP" for n in self._cdp}
            src = "/".join(sorted(kinds))
        card = self._dash_card(
            self.body,
            "همسایگان CDP / LLDP",
            "CDP",
            meta=(f"{len(self._cdp)} · {src}" if self._cdp else "در انتظار / خالی"),
            card_key="cdp",
        )
        card.pack(fill="x", pady=6, padx=2)
        if self._ports_loading and not self._cdp:
            ctk.CTkLabel(
                card,
                text="در حال خواندن CDP/LLDP از SNMP…",
                text_color=self.CARD["muted"],
                anchor="w",
            ).pack(fill="x", padx=12, pady=(0, 10))
            return
        if not self._cdp:
            ctk.CTkLabel(
                card,
                text="همسایه‌ای یافت نشد — CDP/LLDP را روی دستگاه بررسی کنید یا «خواندن پورت‌ها» را دوباره بزنید",
                text_color=self.CARD["muted"],
                anchor="w",
                wraplength=900,
            ).pack(fill="x", padx=12, pady=(0, 10))
            return
        tree_rows = [
            (
                str(n.get("local_if") or "—")[:60],
                str(n.get("device_id") or "—")[:60],
                str(n.get("remote_port") or "—")[:40],
                str(n.get("platform") or "—")[:60],
                str(n.get("ip") or "—")[:40],
                str(n.get("source") or "CDP")[:12],
            )
            for n in self._cdp
        ]
        self._make_detail_tree(
            card,
            [
                ("lif", "Local IF", 140),
                ("nbr", "Neighbor", 200),
                ("rport", "Remote Port", 140),
                ("plat", "Platform", 200),
                ("ip", "IP", 120),
                ("src", "Src", 50),
            ],
            tree_rows,
            height=min(12, max(4, len(tree_rows))),
        )
    def _render(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        renderers = {
            "ports": self._render_ports_section,
            "cpu_mem": self._render_cpu_mem_card,
            "dashboard": self._render_bottom_cards,
            "inventory": self._render_full_inventory,
            "iface": self._render_iface_table,
            "cdp": self._render_cdp_card,
        }
        for key in self._card_order():
            fn = renderers.get(key)
            if fn is not None:
                fn()
        if self.device.last_error:
            err = self._dash_card(self.body, "Monitor Status", "!", meta="")
            err.pack(fill="x", pady=6, padx=2)
            ctk.CTkLabel(
                err,
                text=self.device.last_error,
                text_color=self.CARD["crit"],
                anchor="w",
                wraplength=900,
            ).pack(fill="x", padx=12, pady=(0, 10))
        self.after(80, self._fit_to_content)

    def _load_ports(self) -> None:
        if self._ports_loading:
            return
        is_router = self.device.device_type == "router"
        if self.device.simulate:
            # Simulate stacked switch SW1+SW2 + Po + CDP
            ports = [
                {
                    "label": str(i),
                    "kind": "access",
                    "stack": 1,
                    "port": i,
                    "status": "up" if i % 5 else "down",
                    "speed": "1G" if i % 5 else "100M",
                }
                for i in range(1, 53)
            ] + [
                {
                    "label": str(i),
                    "kind": "access",
                    "stack": 2,
                    "port": i,
                    "status": "up" if i % 4 else "down",
                    "speed": "1G",
                }
                for i in range(1, 53)
            ]
            ports.append(
                {
                    "label": "Po1",
                    "kind": "portchannel",
                    "stack": 0,
                    "port": 1,
                    "status": "up",
                    "speed": "2G",
                }
            )
            if is_router:
                ports.extend(
                    [
                        {
                            "label": f"Tu{i}",
                            "kind": "tunnel",
                            "stack": 0,
                            "port": i,
                            "status": "up" if i % 2 else "down",
                            "speed": "",
                        }
                        for i in range(0, 4)
                    ]
                )
            self._ports = ports
            self._stack = [
                {"num": 1, "role": "master", "is_master": True, "is_active": True, "mac": "0011.2233.4401"},
                {"num": 2, "role": "member", "is_master": False, "is_active": True, "mac": "0011.2233.4402"},
            ]
            self._ifaces = [
                {
                    "name": f"Gi1/0/{i}",
                    "description": f"Port {i} desc",
                    "ip": f"10.0.0.{i}" if i == 1 else "",
                    "status": "up" if i % 5 else "down",
                    "speed": "1G",
                    "in_fmt": f"{i * 12.3:.1f} MB",
                    "out_fmt": f"{i * 8.1:.1f} MB",
                }
                for i in range(1, 25)
            ]
            self._cdp = [
                {
                    "local_if": "Gi1/0/1",
                    "device_id": "SW-Access-1.local",
                    "remote_port": "Gi0/1",
                    "platform": "cisco WS-C2960",
                    "ip": "192.168.20.10",
                },
                {
                    "local_if": "Gi1/0/2",
                    "device_id": "RT-Edge",
                    "remote_port": "Gi0/0",
                    "platform": "cisco ISR4331",
                    "ip": "192.168.20.1",
                },
            ]

            self._render()
            return

        self._ports_loading = True
        self.ports_btn.configure(state="disabled", text="در حال خواندن…")
        self._render()

        host = self.device.host
        community = self.device.community
        port = self.device.port
        include_tunnels = is_router

        def worker() -> None:
            try:
                ports = fetch_ports(
                    host,
                    community,
                    port,
                    timeout=5.0,
                    include_tunnels=include_tunnels,
                )
            except Exception:
                ports = []
            try:
                ifaces = fetch_interfaces(host, community, port, timeout=6.0)
            except Exception:
                ifaces = []
            try:
                stack = fetch_stack(host, community, port, timeout=3.0)
            except Exception:
                stack = []
            try:
                cdp = fetch_cdp(host, community, port, timeout=5.0)
            except Exception:
                cdp = []
            self.after(
                0,
                lambda: self._on_ports_loaded(ports, ifaces, stack, cdp),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_ports_loaded(
        self,
        ports: list,
        ifaces: list | None = None,
        stack: list | None = None,
        cdp: list | None = None,
    ) -> None:
        self._ports_loading = False
        self._ports = ports or []
        if ifaces is not None:
            self._ifaces = ifaces or []
        if stack is not None:
            self._stack = stack or []
        if cdp is not None:
            self._cdp = cdp or []
        try:
            self.ports_btn.configure(state="normal", text="خواندن پورت‌ها")
        except Exception:
            pass
        self._render()

    def _refresh_static(self) -> None:
        if self.on_refresh_static:
            self.on_refresh_static(self.device.id)
            self.after(800, self._poll_device_update)

    def _poll_device_update(self) -> None:
        self._render()


class HoverTip(ctk.CTkToplevel):
    """Lightweight hover card for ranking rows."""

    def __init__(self, master):
        super().__init__(master)
        self.withdraw()
        self.overrideredirect(True)
        self.configure(fg_color=COLORS["panel2"])
        self.attributes("-topmost", True)
        self.label = ctk.CTkLabel(
            self,
            text="",
            justify="right",
            anchor="e",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text"],
        )
        self.label.pack(padx=12, pady=10)
        self._bound = False

    def show_for(self, device: Device, x: int, y: int) -> None:
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        type_fa = TYPE_LABELS.get(device.device_type, "نامشخص")
        model = (device.static_info or {}).get("model") or "—"
        serial = (device.static_info or {}).get("serial") or "—"
        ios = (device.static_info or {}).get("ios_version") or "—"
        uptime = (device.dynamic_info or {}).get("uptime") or "—"
        cpu = (device.dynamic_info or {}).get("cpu_percent")
        mem = (device.dynamic_info or {}).get("memory_percent")
        cpu_s = f"{cpu}%" if cpu is not None else "—"
        mem_s = f"{mem}%" if mem is not None else "—"
        text = (
            f"{device.name}\n"
            f"{type_fa} · {device.host}\n"
            f"مدل: {model}\n"
            f"سریال: {serial}\n"
            f"IOS: {ios}\n"
            f"دما: {temp_label(device.temperature, device.status)}\n"
            f"CPU: {cpu_s}  ·  RAM: {mem_s}\n"
            f"آپتایم: {uptime}"
        )
        try:
            self.label.configure(text=text)
            self.update_idletasks()
            self.geometry(f"+{x + 16}+{y + 16}")
            self.deiconify()
        except Exception:
            pass

    def hide(self) -> None:
        try:
            if self.winfo_exists():
                self.withdraw()
        except Exception:
            pass


class DiscoveryDialog(ctk.CTkToplevel):
    """Scan one or more networks via SNMP and let user pick devices to add."""

    def __init__(self, master, existing_hosts: set[str], default_community: str = "public"):
        super().__init__(master)
        self.title("دیسکاوری شبکه")
        self.geometry("820x700")
        self.minsize(720, 600)
        self.configure(fg_color=COLORS["panel"])
        self.transient(master)
        self.grab_set()

        self.existing_hosts = {h.lower() for h in existing_hosts}
        self.selected: list[DiscoveredHost] = []
        self.community_value = default_community
        self.port_value = 161
        self._rows: list[tuple[ctk.BooleanVar, DiscoveredHost, ctk.CTkCheckBox]] = []
        self._scanning = False
        self._found: list[DiscoveredHost] = []
        self._saved = False

        ctk.CTkLabel(
            self,
            text="دیسکاوری SNMP شبکه",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).pack(anchor="e", padx=20, pady=(16, 4))

        ctk.CTkLabel(
            self,
            text="چند شبکه را خط‌به‌خط وارد کنید، اسکن کنید، بعد «ذخیره» را بزنید",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
        ).pack(anchor="e", padx=20)

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=20, pady=10)

        ctk.CTkLabel(form, text="شبکه‌ها", text_color=COLORS["muted"], anchor="e").pack(
            fill="x"
        )
        self.networks_box = ctk.CTkTextbox(form, height=70, font=ctk.CTkFont(size=13))
        self.networks_box.pack(fill="x", pady=(2, 8))
        self.networks_box.insert("1.0", "192.168.1.0/24\n10.0.0.0/24")

        row = ctk.CTkFrame(form, fg_color="transparent")
        row.pack(fill="x")

        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(
            left, text="SNMP Community / Key", text_color=COLORS["muted"], anchor="e"
        ).pack(fill="x")
        self.comm_var = ctk.StringVar(value=default_community)
        ctk.CTkEntry(left, textvariable=self.comm_var, justify="left", height=34).pack(
            fill="x"
        )

        mid = ctk.CTkFrame(row, fg_color="transparent")
        mid.pack(side="left", padx=8)
        ctk.CTkLabel(mid, text="پورت", text_color=COLORS["muted"]).pack()
        self.port_var = ctk.StringVar(value="161")
        ctk.CTkEntry(
            mid, textvariable=self.port_var, width=70, height=34, justify="center"
        ).pack()

        right = ctk.CTkFrame(row, fg_color="transparent")
        right.pack(side="left", padx=8)
        ctk.CTkLabel(right, text="Timeout (ثانیه)", text_color=COLORS["muted"]).pack()
        self.timeout_var = ctk.StringVar(value="0.6")
        ctk.CTkEntry(
            right, textvariable=self.timeout_var, width=80, height=34, justify="center"
        ).pack()

        opts = ctk.CTkFrame(form, fg_color="transparent")
        opts.pack(fill="x", pady=(10, 0))
        self.cisco_only = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            opts,
            text="فقط دستگاه‌های سیسکو",
            variable=self.cisco_only,
        ).pack(side="right")

        actions = ctk.CTkFrame(form, fg_color="transparent")
        actions.pack(fill="x", pady=(12, 0))
        self.scan_btn = ctk.CTkButton(
            actions,
            text="شروع اسکن",
            width=130,
            fg_color=COLORS["accent"],
            command=self._start_scan,
        )
        self.scan_btn.pack(side="right")
        self.progress_lbl = ctk.CTkLabel(
            actions, text="آماده اسکن", text_color=COLORS["muted"], anchor="e"
        )
        self.progress_lbl.pack(side="right", padx=12, fill="x", expand=True)

        select_bar = ctk.CTkFrame(self, fg_color="transparent")
        select_bar.pack(fill="x", padx=20, pady=(4, 0))
        ctk.CTkButton(
            select_bar,
            text="انتخاب همه جدید",
            width=120,
            height=28,
            fg_color=COLORS["panel2"],
            command=self._select_new,
        ).pack(side="right")
        ctk.CTkButton(
            select_bar,
            text="فقط سیسکو",
            width=100,
            height=28,
            fg_color=COLORS["panel2"],
            command=self._select_cisco,
        ).pack(side="right", padx=6)
        ctk.CTkButton(
            select_bar,
            text="حذف انتخاب",
            width=100,
            height=28,
            fg_color=COLORS["panel2"],
            command=self._clear_selection,
        ).pack(side="right")
        self.count_lbl = ctk.CTkLabel(
            select_bar, text="", text_color=COLORS["muted"], anchor="e"
        )
        self.count_lbl.pack(side="left")

        # Footer FIRST (bottom) so Save/Cancel never get crushed
        footer = ctk.CTkFrame(self, fg_color=COLORS["panel2"], corner_radius=12, height=72)
        footer.pack(side="bottom", fill="x", padx=20, pady=(8, 16))
        footer.pack_propagate(False)

        self.save_btn = ctk.CTkButton(
            footer,
            text="ذخیره دستگاه‌های انتخاب‌شده",
            height=44,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=COLORS["ok"],
            hover_color="#27ae60",
            text_color="#04140a",
            command=self._accept,
        )
        self.save_btn.pack(side="right", padx=12, pady=12)

        ctk.CTkButton(
            footer,
            text="انصراف",
            width=100,
            height=36,
            fg_color="#3a4558",
            command=self._cancel,
        ).pack(side="left", padx=12, pady=12)

        self.save_hint = ctk.CTkLabel(
            footer,
            text="بعد از اسکن، تیک بزنید و ذخیره را بزنید",
            text_color=COLORS["muted"],
            anchor="e",
        )
        self.save_hint.pack(side="right", fill="x", expand=True, padx=8)

        # Results fill remaining space above the footer
        self.results = ctk.CTkScrollableFrame(
            self, fg_color=COLORS["bg"], corner_radius=10
        )
        self.results.pack(fill="both", expand=True, padx=20, pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.after(40, self._center)

    def _center(self) -> None:
        self.update_idletasks()
        if self.master.winfo_ismapped():
            x = self.master.winfo_rootx() + (
                self.master.winfo_width() - self.winfo_width()
            ) // 2
            y = self.master.winfo_rooty() + (
                self.master.winfo_height() - self.winfo_height()
            ) // 2
            self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _chosen(self) -> list[DiscoveredHost]:
        return [host for var, host, _cb in self._rows if var.get()]

    def _new_hosts(self) -> list[DiscoveredHost]:
        return [h for h in self._found if h.ip.lower() not in self.existing_hosts]

    def _update_count(self) -> None:
        chosen = len(self._chosen())
        total_new = len(self._new_hosts())
        self.count_lbl.configure(text=f"انتخاب‌شده: {chosen} از {total_new} دستگاه جدید")
        if chosen:
            self.save_hint.configure(
                text=f"{chosen} دستگاه آماده ذخیره است — دکمه سبز را بزنید",
                text_color=COLORS["ok"],
            )
        elif self._found:
            self.save_hint.configure(
                text="هیچ دستگاهی تیک نخورده — «انتخاب همه جدید» را بزنید",
                text_color=COLORS["warn"],
            )
        else:
            self.save_hint.configure(
                text="بعد از اسکن، تیک بزنید و ذخیره را بزنید",
                text_color=COLORS["muted"],
            )

    def _start_scan(self) -> None:
        if self._scanning:
            return
        networks_text = self.networks_box.get("1.0", "end").strip()
        community = self.comm_var.get().strip() or "public"
        try:
            nets = parse_networks(networks_text)
            port = int(self.port_var.get().strip())
            timeout = float(self.timeout_var.get().strip())
        except ValueError as exc:
            messagebox.showerror("خطا", str(exc), parent=self)
            return

        host_count = len(iter_hosts(nets))
        if host_count > 2048:
            ok = messagebox.askyesno(
                "هشدار",
                f"حدود {host_count} آدرس اسکن می‌شود و ممکن است طول بکشد.\nادامه می‌دهید؟",
                parent=self,
            )
            if not ok:
                return
        elif host_count == 0:
            messagebox.showerror("خطا", "آدرسی برای اسکن وجود ندارد.", parent=self)
            return

        self._scanning = True
        self.scan_btn.configure(state="disabled", text="در حال اسکن…")
        self.save_btn.configure(state="disabled")
        self.progress_lbl.configure(text="شروع اسکن…")
        for child in self.results.winfo_children():
            child.destroy()
        self._rows.clear()
        self._found.clear()
        self._update_count()

        def progress(done: int, total: int, host: Optional[DiscoveredHost]) -> None:
            # Bind host into default arg to avoid late-binding bug in loop
            self.after(0, lambda d=done, t=total, h=host: self._on_progress(d, t, h))

        def worker() -> None:
            try:
                found = discover_networks(
                    networks_text=networks_text,
                    community=community,
                    port=port,
                    timeout=timeout,
                    concurrency=80,
                    cisco_only=bool(self.cisco_only.get()),
                    progress_cb=progress,
                )
                self.after(0, lambda f=found: self._on_scan_done(f, None))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._on_scan_done([], e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_progress(self, done: int, total: int, host: Optional[DiscoveredHost]) -> None:
        pct = int((done / total) * 100) if total else 0
        extra = f" — پیدا شد: {host.display_name}" if host else ""
        self.progress_lbl.configure(text=f"اسکن {done}/{total} ({pct}%){extra}")
        if host and not any(h.ip == host.ip for h in self._found):
            self._found.append(host)
            self._add_result_row(host)
            self._update_count()

    def _on_scan_done(self, found: list[DiscoveredHost], error: Optional[str]) -> None:
        self._scanning = False
        self.scan_btn.configure(state="normal", text="شروع اسکن")
        self.save_btn.configure(state="normal")
        if error:
            self.progress_lbl.configure(text=f"خطا: {error}")
            messagebox.showerror("خطا", error, parent=self)
            return
        for child in self.results.winfo_children():
            child.destroy()
        self._rows.clear()
        self._found = found
        for host in found:
            self._add_result_row(host)
        # Always pre-select every newly discovered device
        self._select_new()
        new_count = len(self._new_hosts())
        self.progress_lbl.configure(
            text=f"تمام — {len(found)} پیدا شد · {new_count} جدید آماده ذخیره"
        )
        self._update_count()
        if new_count:
            self.save_btn.configure(
                text=f"ذخیره {new_count} دستگاه انتخاب‌شده",
            )

    def _add_result_row(self, host: DiscoveredHost) -> None:
        already = host.ip.lower() in self.existing_hosts
        # Default: select all NEW devices (cisco or not)
        var = ctk.BooleanVar(value=not already)
        row = ctk.CTkFrame(self.results, fg_color=COLORS["panel2"], corner_radius=8)
        row.pack(fill="x", pady=3, padx=4)

        cb = ctk.CTkCheckBox(
            row,
            text="",
            variable=var,
            width=28,
            command=self._update_count,
        )
        if already:
            cb.configure(state="disabled")
            var.set(False)
        cb.pack(side="left", padx=(8, 0), pady=8)

        badge_color = COLORS["accent"] if host.is_cisco else COLORS["muted"]
        badge_text = "Cisco" if host.is_cisco else "SNMP"
        if already:
            badge_text = "قبلاً هست"
            badge_color = COLORS["warn"]

        ctk.CTkLabel(
            row,
            text=badge_text,
            width=70,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=badge_color,
        ).pack(side="left", padx=4)

        info = ctk.CTkFrame(row, fg_color="transparent")
        info.pack(side="right", fill="x", expand=True, padx=10, pady=6)
        ctk.CTkLabel(
            info,
            text=f"{host.display_name}  ·  {host.ip}",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="e",
            text_color=COLORS["text"],
        ).pack(fill="x")
        descr = (host.sys_descr or "")[:90]
        meta = f"{host.network}" + (f"  ·  {descr}" if descr else "")
        ctk.CTkLabel(
            info,
            text=meta,
            font=ctk.CTkFont(size=11),
            anchor="e",
            text_color=COLORS["muted"],
        ).pack(fill="x")

        self._rows.append((var, host, cb))

    def _select_new(self) -> None:
        for var, host, cb in self._rows:
            if host.ip.lower() not in self.existing_hosts and str(cb.cget("state")) != "disabled":
                var.set(True)
        self._update_count()
        n = len(self._chosen())
        if n:
            self.save_btn.configure(text=f"ذخیره {n} دستگاه انتخاب‌شده")

    def _select_cisco(self) -> None:
        for var, host, cb in self._rows:
            if str(cb.cget("state")) == "disabled":
                continue
            var.set(host.is_cisco and host.ip.lower() not in self.existing_hosts)
        self._update_count()
        n = len(self._chosen())
        self.save_btn.configure(
            text=f"ذخیره {n} دستگاه انتخاب‌شده" if n else "ذخیره دستگاه‌های انتخاب‌شده"
        )

    def _clear_selection(self) -> None:
        for var, _host, cb in self._rows:
            if str(cb.cget("state")) != "disabled":
                var.set(False)
        self._update_count()
        self.save_btn.configure(text="ذخیره دستگاه‌های انتخاب‌شده")

    def _accept(self) -> None:
        chosen = self._chosen()
        if not chosen:
            # Fallback: if user forgot to tick, offer all new devices
            new_hosts = self._new_hosts()
            if not new_hosts:
                messagebox.showinfo(
                    "توجه",
                    "دستگاه جدیدی برای ذخیره نیست.",
                    parent=self,
                )
                return
            if messagebox.askyesno(
                "ذخیره",
                f"هیچ تیکی نزده‌اید. هر {len(new_hosts)} دستگاه جدید ذخیره شود؟",
                parent=self,
            ):
                chosen = new_hosts
            else:
                return

        self.community_value = self.comm_var.get().strip() or "public"
        try:
            self.port_value = int(self.port_var.get().strip())
        except ValueError:
            self.port_value = 161
        self.selected = chosen
        self._saved = True
        self.destroy()

    def _cancel(self) -> None:
        if self._scanning:
            messagebox.showinfo("توجه", "صبر کنید تا اسکن تمام شود.", parent=self)
            return
        if self._saved:
            self.destroy()
            return
        new_hosts = self._new_hosts()
        if new_hosts:
            answer = messagebox.askyesnocancel(
                "ذخیره نشده",
                f"{len(new_hosts)} دستگاه پیدا شده ذخیره نشده.\n\n"
                "Yes = ذخیره همه جدیدها\n"
                "No = بستن بدون ذخیره\n"
                "Cancel = ماندن در پنجره",
                parent=self,
            )
            if answer is True:
                for var, host, cb in self._rows:
                    if host.ip.lower() not in self.existing_hosts and str(
                        cb.cget("state")
                    ) != "disabled":
                        var.set(True)
                self._accept()
                return
            if answer is None:
                return
        self.selected = []
        self.destroy()


class TempMonitorApp(ctk.CTk):
    STRIP_W = 420
    FULL_GEOM = "1240x740"
    SCREEN_MARGIN = 8

    def __init__(self) -> None:
        super().__init__()
        self.title(f"مانیتور دمای سیسکو | v{APP_VERSION}")
        self.configure(fg_color=COLORS["bg"])

        self.app_data = load_state()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._device_cards: dict[str, ctk.CTkFrame] = {}
        self._rank_rows: dict[str, ctk.CTkFrame] = {}
        self._poll_tick = 0
        self._last_rank_key: tuple = ()
        self._last_order_key: tuple = ()
        self._force_inventory = False
        self._hover = None
        self._search_var = ctk.StringVar(value="")
        self._active_query = ""
        self._inventory_busy = False
        self._refresh_after_id = None
        self._alive = True
        self._strip_mode = bool(self.app_data.monitor_strip_mode)
        self._full_geom = self.FULL_GEOM

        self._build_ui()
        self._hover = HoverTip(self)
        self._render_devices()
        self._render_ranking()
        self._update_poll_info()
        self.after(80, self._apply_ui_mode)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._temp_poller = threading.Thread(target=self._temp_poll_loop, daemon=True)
        self._inv_poller = threading.Thread(target=self._inventory_poll_loop, daemon=True)
        self._cfg_poller = threading.Thread(target=self._config_backup_loop, daemon=True)
        self._temp_poller.start()
        self._inv_poller.start()
        self._cfg_poller.start()
        self._refresh_after_id = self.after(800, self._schedule_refresh)

    def _update_poll_info(self) -> None:
        t = self.app_data.temp_poll_seconds
        d = self.app_data.dynamic_poll_seconds
        c = self.app_data.config_backup_hours
        cfg = "خاموش" if c <= 0 else f"{c}س"
        txt = f"دما {t}ث · CPU/RAM {d}ث · Config {cfg}"
        try:
            self.poll_info.configure(text=txt)
        except Exception:
            pass
        try:
            self.poll_info_strip.configure(text=txt)
        except Exception:
            pass

    def _build_ui(self) -> None:
        # —— Compact header (always visible) ——
        self.header = ctk.CTkFrame(self, fg_color=COLORS["panel"], height=52, corner_radius=0)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self.mode_btn = ctk.CTkButton(
            self.header,
            text="◂ باز",
            width=88,
            height=32,
            fg_color=COLORS["accent"],
            hover_color="#4aadf5",
            command=self._toggle_ui_mode,
        )
        self.mode_btn.pack(side="left", padx=10, pady=10)

        self.status_lbl = ctk.CTkLabel(
            self.header,
            text="…",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["muted"],
            anchor="w",
        )
        self.status_lbl.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            self.header,
            text=f"مانیتور دما  v{APP_VERSION}",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=COLORS["text"],
        ).pack(side="right", padx=14)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=8, pady=8)

        # —— Devices panel (full mode only, left) ——
        self.devices_panel = ctk.CTkFrame(
            self.body, fg_color=COLORS["panel"], corner_radius=12
        )

        dev_head = ctk.CTkFrame(self.devices_panel, fg_color="transparent")
        dev_head.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            dev_head,
            text="مدیریت دستگاه‌ها",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLORS["text"],
            anchor="e",
        ).pack(fill="x")

        toolbar = ctk.CTkFrame(self.devices_panel, fg_color="transparent")
        toolbar.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkButton(
            toolbar,
            text="+ افزودن دستگاه",
            width=130,
            height=32,
            fg_color=COLORS["accent"],
            command=self._add_device,
        ).pack(side="right")
        ctk.CTkButton(
            toolbar,
            text="دیسکاوری شبکه",
            width=120,
            height=32,
            fg_color="#1f6f5b",
            hover_color="#26856d",
            command=self._discover_devices,
        ).pack(side="right", padx=(0, 6))
        ctk.CTkButton(
            toolbar,
            text="بکاپ کانفیگ همه",
            width=130,
            height=32,
            fg_color="#6b4f1d",
            hover_color="#8a6524",
            command=self._backup_all_configs,
        ).pack(side="right", padx=(0, 6))
        ctk.CTkButton(
            toolbar,
            text="تنظیمات",
            width=80,
            height=32,
            fg_color=COLORS["panel2"],
            command=self._open_settings,
        ).pack(side="left")

        search_row = ctk.CTkFrame(self.devices_panel, fg_color="transparent")
        search_row.pack(fill="x", padx=12, pady=(0, 8))
        self.search_entry = ctk.CTkEntry(
            search_row,
            textvariable=self._search_var,
            placeholder_text="جستجو نام / IP / مدل…",
            height=32,
            justify="right",
        )
        self.search_entry.pack(side="right", fill="x", expand=True, padx=(6, 0))
        self.search_entry.bind("<Return>", lambda _e: self._apply_search())
        ctk.CTkButton(
            search_row,
            text="جستجو",
            width=70,
            height=32,
            fg_color=COLORS["accent"],
            command=self._apply_search,
        ).pack(side="right")
        ctk.CTkButton(
            search_row,
            text="پاک",
            width=50,
            height=32,
            fg_color=COLORS["panel2"],
            command=self._clear_search,
        ).pack(side="left")

        self.device_scroll = ctk.CTkScrollableFrame(
            self.devices_panel, fg_color="transparent", corner_radius=0
        )
        self.device_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 10))

        # —— Rank / strip panel (always visible, right or full) ——
        self.rank_panel = ctk.CTkFrame(
            self.body, fg_color=COLORS["panel"], corner_radius=12
        )

        rank_tools = ctk.CTkFrame(self.rank_panel, fg_color="transparent")
        rank_tools.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkButton(
            rank_tools,
            text="بروزرسانی",
            width=80,
            height=28,
            fg_color=COLORS["panel2"],
            command=self._force_poll,
        ).pack(side="left")
        self.poll_info_strip = ctk.CTkLabel(
            rank_tools,
            text="",
            font=ctk.CTkFont(size=10),
            text_color=COLORS["muted"],
            anchor="e",
        )
        self.poll_info_strip.pack(side="right", fill="x", expand=True)
        # alias used by _update_poll_info
        self.poll_info = self.poll_info_strip

        ctk.CTkLabel(
            self.rank_panel,
            text="گرم‌ترین ← خنک‌ترین  ·  موس=جزئیات  ·  دبل‌کلیک=صفحه کامل",
            font=ctk.CTkFont(size=10),
            text_color=COLORS["muted"],
            anchor="e",
        ).pack(fill="x", padx=10, pady=(0, 4))

        self.rank_scroll = ctk.CTkScrollableFrame(
            self.rank_panel, fg_color="transparent", corner_radius=0
        )
        self.rank_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # —— Legend (full mode) ——
        self.legend = ctk.CTkFrame(
            self, fg_color=COLORS["panel"], height=36, corner_radius=0
        )
        self.legend.pack_propagate(False)
        items = [
            (COLORS["ok"], f"<{TEMP_OK}"),
            (COLORS["warn"], f"{TEMP_OK}–{TEMP_WARN}"),
            (COLORS["hot"], f"{TEMP_WARN}–{TEMP_CRIT}"),
            (COLORS["crit"], f"≥{TEMP_CRIT}"),
        ]
        for color, text in items:
            box = ctk.CTkFrame(self.legend, fg_color="transparent")
            box.pack(side="right", padx=10, pady=6)
            ctk.CTkFrame(box, width=10, height=10, fg_color=color, corner_radius=2).pack(
                side="right", padx=(4, 0)
            )
            ctk.CTkLabel(
                box, text=text, font=ctk.CTkFont(size=10), text_color=COLORS["muted"]
            ).pack(side="right")

    def _toggle_ui_mode(self) -> None:
        self._set_ui_mode(not self._strip_mode)

    def _set_ui_mode(self, strip: bool) -> None:
        if not self._alive:
            return
        # Remember full window size before collapsing to strip
        if self._strip_mode is False and strip:
            try:
                self._full_geom = self.geometry().split("+")[0]
            except Exception:
                self._full_geom = self.FULL_GEOM
        self._strip_mode = bool(strip)
        self.app_data.monitor_strip_mode = self._strip_mode
        try:
            save_state(self.app_data)
        except Exception:
            pass
        self._apply_ui_mode()

    def _apply_ui_mode(self) -> None:
        """Strip = thin right-edge bar; Full = classic management + monitor."""
        if not self._alive:
            return

        # Clear body packing
        try:
            self.devices_panel.pack_forget()
        except Exception:
            pass
        try:
            self.rank_panel.pack_forget()
        except Exception:
            pass
        try:
            self.legend.pack_forget()
        except Exception:
            pass

        if self._strip_mode:
            self.mode_btn.configure(text="◂ باز")
            self.title(f"دما | Cisco Strip  v{APP_VERSION}")
            try:
                self.rank_panel.configure(width=self.STRIP_W)
                self.rank_panel.pack_propagate(True)
            except Exception:
                pass
            self.rank_panel.pack(fill="both", expand=True)
            self._dock_strip_window()
        else:
            self.mode_btn.configure(text="▸ نوار")
            self.title(f"مانیتور دمای سیسکو | v{APP_VERSION}")
            self.devices_panel.pack(side="left", fill="both", expand=True, padx=(0, 8))
            self.rank_panel.configure(width=self.STRIP_W)
            self.rank_panel.pack_propagate(False)
            self.rank_panel.pack(side="right", fill="y")
            self.legend.pack(fill="x", side="bottom")
            self._open_full_window()

    def _dock_strip_window(self) -> None:
        """Thin strip parked in the top-right corner of the screen."""
        try:
            sw = int(self.winfo_screenwidth())
            sh = int(self.winfo_screenheight())
        except Exception:
            sw, sh = 1920, 1080
        margin = self.SCREEN_MARGIN
        w = self.STRIP_W
        # Top-right corner — not full screen height
        h = max(460, min(int(sh * 0.62), sh - 72))
        x = max(0, sw - w - margin)
        y = margin
        self._anchor_right = x + w
        self._anchor_top = y
        self.minsize(360, 400)
        self.maxsize(w + 20, sh)
        self.geometry(f"{w}x{h}+{x}+{y}")
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass
        try:
            self.resizable(False, True)
        except Exception:
            pass

    def _open_full_window(self) -> None:
        """Expand to the left from the strip's right edge (NOC dock behavior)."""
        geom = self._full_geom or self.FULL_GEOM
        self.minsize(1000, 620)
        try:
            self.maxsize(10000, 10000)
        except Exception:
            pass
        try:
            self.resizable(True, True)
        except Exception:
            pass
        try:
            self.attributes("-topmost", False)
        except Exception:
            pass
        try:
            sw = int(self.winfo_screenwidth())
            sh = int(self.winfo_screenheight())
            parts = geom.replace("x", "+").split("+")
            ww = int(parts[0]) if parts else 1240
            hh = int(parts[1]) if len(parts) > 1 else 740
            margin = self.SCREEN_MARGIN
            right = int(getattr(self, "_anchor_right", sw - margin))
            top = int(getattr(self, "_anchor_top", margin))
            # Keep right edge fixed → grow leftward
            ww = min(ww, right - margin)
            hh = min(hh, sh - top - 48)
            ww = max(1000, ww)
            hh = max(620, hh)
            x = max(margin, right - ww)
            y = top
            # If screen is too narrow, clamp width
            if x + ww > sw - margin:
                ww = max(800, sw - margin - x)
            self.geometry(f"{ww}x{hh}+{x}+{y}")
        except Exception:
            self.geometry(self.FULL_GEOM)

    def _open_settings(self) -> None:
        if not getattr(self, "_alive", True):
            return
        dlg = SettingsDialog(self, self.app_data)
        self.wait_window(dlg)
        if not dlg.result:
            return
        with self._lock:
            self.app_data.temp_poll_seconds = dlg.result["temp_poll_seconds"]
            self.app_data.dynamic_poll_seconds = dlg.result["dynamic_poll_seconds"]
            self.app_data.config_backup_hours = int(
                dlg.result.get("config_backup_hours", CONFIG_BACKUP_HOURS)
            )
            save_state(self.app_data)
        self._update_poll_info()

    def _add_device(self) -> None:
        dlg = DeviceDialog(self, "افزودن دستگاه")
        self.wait_window(dlg)
        if not dlg.result:
            return
        device = Device(
            id=str(uuid.uuid4()),
            name=dlg.result["name"],
            host=dlg.result["host"],
            community=dlg.result["community"],
            port=dlg.result["port"],
            simulate=dlg.result["simulate"],
            sim_base=38 + (hash(dlg.result["host"]) % 20),
            ssh_user=dlg.result.get("ssh_user", ""),
            ssh_password=dlg.result.get("ssh_password", ""),
            ssh_port=int(dlg.result.get("ssh_port") or 22),
            ssh_enable=dlg.result.get("ssh_enable", ""),
        )
        with self._lock:
            self.app_data.devices.append(device)
            save_state(self.app_data)
        self._render_devices()
        self._render_ranking()
        self._force_poll()

    def _discover_devices(self) -> None:
        with self._lock:
            existing = {d.host for d in self.app_data.devices}
            default_comm = next(
                (d.community for d in self.app_data.devices if not d.simulate),
                "public",
            )
        dlg = DiscoveryDialog(self, existing_hosts=existing, default_community=default_comm)
        self.wait_window(dlg)
        if not dlg.selected:
            return

        community = getattr(dlg, "community_value", None) or "public"
        port = int(getattr(dlg, "port_value", 161) or 161)

        added = 0
        with self._lock:
            known = {d.host.lower() for d in self.app_data.devices}
            for host in dlg.selected:
                if host.ip.lower() in known:
                    continue
                self.app_data.devices.append(
                    Device(
                        id=str(uuid.uuid4()),
                        name=host.display_name,
                        host=host.ip,
                        community=community,
                        port=port,
                        simulate=False,
                    )
                )
                known.add(host.ip.lower())
                added += 1
            save_state(self.app_data)

        self._render_devices()
        self._render_ranking()
        if added:
            self._force_poll()
            messagebox.showinfo(
                "دیسکاوری", f"{added} دستگاه ذخیره و اضافه شد.", parent=self
            )
        else:
            messagebox.showinfo(
                "دیسکاوری", "دستگاه جدیدی برای افزودن نبود.", parent=self
            )

    def _edit_device(self, device_id: str) -> None:
        with self._lock:
            device = next((d for d in self.app_data.devices if d.id == device_id), None)
        if not device:
            return
        dlg = DeviceDialog(self, "ویرایش دستگاه", device)
        self.wait_window(dlg)
        if not dlg.result:
            return
        with self._lock:
            device.name = dlg.result["name"]
            device.host = dlg.result["host"]
            device.community = dlg.result["community"]
            device.port = dlg.result["port"]
            device.simulate = dlg.result["simulate"]
            device.ssh_user = dlg.result.get("ssh_user", "")
            device.ssh_password = dlg.result.get("ssh_password", "")
            device.ssh_port = int(dlg.result.get("ssh_port") or 22)
            device.ssh_enable = dlg.result.get("ssh_enable", "")
            device.status = "pending"
            device.temperature = None
            device.static_fetched_at = 0.0
            device.dynamic_fetched_at = 0.0
            save_state(self.app_data)
        self._render_devices()
        self._render_ranking()
        self._force_poll()

    def _delete_device(self, device_id: str) -> None:
        if not messagebox.askyesno("حذف", "این دستگاه حذف شود؟", parent=self):
            return
        with self._lock:
            self.app_data.devices = [
                d for d in self.app_data.devices if d.id != device_id
            ]
            save_state(self.app_data)
        self._render_devices()
        self._render_ranking()

    def _open_detail(self, device_id: str) -> None:
        if not getattr(self, "_alive", True):
            return
        with self._lock:
            device = next((d for d in self.app_data.devices if d.id == device_id), None)
        if not device:
            return
        DetailDialog(self, device, on_refresh_static=self._refresh_static_async)

    def _open_config_browser(self, device_id: str) -> None:
        if not getattr(self, "_alive", True):
            return
        with self._lock:
            device = next((d for d in self.app_data.devices if d.id == device_id), None)
        if not device:
            return
        if not device.simulate and not (device.ssh_user and device.ssh_password):
            if not messagebox.askyesno(
                "SSH",
                "برای بکاپ کانفیگ، SSH Username/Password لازم است.\n"
                "الان فقط تاریخچه ذخیره‌شده باز شود؟\n\n"
                "(برای تنظیم SSH از «ویرایش» استفاده کنید)",
                parent=self,
            ):
                return
        ConfigBrowserDialog(self, device)

    def _backup_one_device(self, device: Device):
        return backup_device_config(
            device_id=device.id,
            device_name=device.name,
            host=device.host,
            username=device.ssh_user,
            password=device.ssh_password,
            ssh_port=device.ssh_port or 22,
            enable_secret=device.ssh_enable,
            simulate=device.simulate,
        )

    def _backup_all_configs(self) -> None:
        if not self._alive:
            return
        with self._lock:
            devices = list(self.app_data.devices)
        if not devices:
            messagebox.showinfo("بکاپ", "دستگاهی نیست.", parent=self)
            return
        self.status_lbl.configure(text="بکاپ کانفیگ همه دستگاه‌ها…")

        def worker() -> None:
            ok_n = 0
            fail_n = 0
            changed = 0
            for d in devices:
                if self._stop.is_set():
                    break
                if not d.simulate and not (d.ssh_user and d.ssh_password):
                    fail_n += 1
                    continue
                result = self._backup_one_device(d)
                if result.ok:
                    ok_n += 1
                    if result.running_changed or result.startup_differs:
                        changed += 1
                    with self._lock:
                        d.config_backed_up_at = time.time()
                else:
                    fail_n += 1
            try:
                save_state(self.app_data)
            except Exception:
                pass
            msg = f"بکاپ کانفیگ: موفق {ok_n} · ناموفق {fail_n} · تغییر/اختلاف {changed}"
            self.after(0, lambda: self._on_backup_all_done(msg))

        threading.Thread(target=worker, daemon=True).start()

    def _on_backup_all_done(self, msg: str) -> None:
        if not self._alive:
            return
        try:
            self.status_lbl.configure(text=msg)
        except Exception:
            pass
        messagebox.showinfo("بکاپ کانفیگ", msg, parent=self)

    def _config_backup_loop(self) -> None:
        """Scheduled config backups (hours interval; 0 = off)."""
        while not self._stop.is_set():
            try:
                hours = int(self.app_data.config_backup_hours or 0)
            except Exception:
                hours = 0
            if hours <= 0:
                self._stop.wait(60)
                continue
            interval = max(1, hours) * 3600
            due: list[Device] = []
            now = time.time()
            with self._lock:
                for d in self.app_data.devices:
                    if not d.simulate and not (d.ssh_user and d.ssh_password):
                        continue
                    last = float(d.config_backed_up_at or 0)
                    if last <= 0 or (now - last) >= interval:
                        due.append(d)
            for d in due:
                if self._stop.is_set():
                    break
                try:
                    result = self._backup_one_device(d)
                    if result.ok:
                        with self._lock:
                            d.config_backed_up_at = time.time()
                        try:
                            save_state(self.app_data)
                        except Exception:
                            pass
                        note = f"Config backup: {d.name}"
                        if result.startup_differs:
                            note += " · Running≠Startup"
                        if result.running_changed:
                            note += " · Running changed"
                        if self._alive:
                            self.after(
                                0,
                                lambda m=note: self.status_lbl.configure(text=m)
                                if self._alive
                                else None,
                            )
                except Exception:
                    pass
            self._stop.wait(300)

    def _refresh_static_async(self, device_id: str) -> None:
        def worker() -> None:
            with self._lock:
                device = next(
                    (d for d in self.app_data.devices if d.id == device_id), None
                )
            if not device or device.simulate:
                return
            inv = fetch_static(device.host, device.community, device.port)
            with self._lock:
                if inv.ok:
                    device.static_info = inv.static
                    device.device_type = inv.device_type
                    device.static_fetched_at = time.time()
                    if inv.static.get("sys_name") and device.name == device.host:
                        device.name = inv.static["sys_name"]
                    save_state(self.app_data)
            self.after(0, self._render_devices)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_search(self) -> None:
        self._active_query = self._search_var.get().strip()
        self._render_devices()

    def _clear_search(self) -> None:
        self._search_var.set("")
        self._active_query = ""
        self._render_devices()

    def _device_matches_search(self, device: Device, query: str) -> bool:
        if not query:
            return True
        q = query.lower().strip()
        blob = " ".join(
            [
                device.name or "",
                device.host or "",
                device.device_type or "",
                TYPE_LABELS.get(device.device_type, ""),
                str((device.static_info or {}).get("model") or ""),
                str((device.static_info or {}).get("serial") or ""),
                str((device.static_info or {}).get("sys_location") or ""),
            ]
        ).lower()
        return q in blob

    def _render_devices(self) -> None:
        for child in self.device_scroll.winfo_children():
            child.destroy()
        self._device_cards.clear()

        query = self._active_query
        with self._lock:
            devices = [
                d for d in self.app_data.devices if self._device_matches_search(d, query)
            ]

        if not self.app_data.devices:
            empty = ctk.CTkFrame(
                self.device_scroll, fg_color=COLORS["panel"], corner_radius=14
            )
            empty.pack(fill="x", pady=8)
            ctk.CTkLabel(
                empty,
                text="هنوز دستگاهی اضافه نشده.\nروی «افزودن» یا «دیسکاوری» کلیک کنید.",
                font=ctk.CTkFont(size=15),
                text_color=COLORS["muted"],
                justify="center",
            ).pack(padx=24, pady=40)
            return

        if not devices:
            ctk.CTkLabel(
                self.device_scroll,
                text=f"نتیجه‌ای برای «{query}» پیدا نشد",
                text_color=COLORS["muted"],
            ).pack(pady=30)
            return

        for device in devices:
            card = self._make_device_card(device)
            card.pack(fill="x", pady=6)
            self._device_cards[device.id] = card

    def _fmt_metrics(self, device: Device) -> tuple[str, str, str, str]:
        dyn = device.dynamic_info or {}
        temp = device.temperature
        cpu = dyn.get("cpu_percent")
        ram = dyn.get("memory_percent")
        poe_w = dyn.get("poe_used_w")
        poe = dyn.get("poe_percent")
        t_val = f"{temp:.0f}°" if temp is not None else "—"
        c_val = f"{cpu:.0f}%" if cpu is not None else "—"
        r_val = f"{ram:.0f}%" if ram is not None else "—"
        if poe_w is not None:
            watts = float(poe_w)
            if watts >= 1000:
                p_val = f"{watts / 1000:.1f}kW"
            else:
                p_val = f"{watts:.0f}W"
        elif poe is not None:
            p_val = f"{float(poe):.0f}%"
        else:
            p_val = "—"
        return t_val, c_val, r_val, p_val

    def _make_device_card(self, device: Device) -> ctk.CTkFrame:
        """Lightweight left card — no live metric chips (keeps buttons clickable)."""
        card = ctk.CTkFrame(
            self.device_scroll,
            fg_color=COLORS["panel"],
            corner_radius=12,
            border_width=1,
            border_color=COLORS["border"],
        )

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill="x", padx=14, pady=(12, 4))

        type_code, type_color = TYPE_BADGE.get(
            device.device_type, TYPE_BADGE["unknown"]
        )
        type_badge = ctk.CTkLabel(
            top,
            text=type_code,
            width=44,
            height=44,
            corner_radius=8,
            fg_color=type_color,
            text_color="#041018",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        type_badge.pack(side="left")

        info = ctk.CTkFrame(top, fg_color="transparent")
        info.pack(side="right", fill="x", expand=True, padx=(10, 0))

        name_lbl = ctk.CTkLabel(
            info,
            text=device.name,
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=COLORS["text"],
            anchor="e",
        )
        name_lbl.pack(fill="x")

        type_fa = TYPE_LABELS.get(device.device_type, "نامشخص")
        model = (device.static_info or {}).get("model") or ""
        meta = f"{type_fa}  ·  {device.host}"
        if model:
            meta += f"  ·  {model}"
        if device.simulate:
            meta += "  ·  شبیه‌سازی"
        meta_lbl = ctk.CTkLabel(
            info,
            text=meta,
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
            anchor="e",
        )
        meta_lbl.pack(fill="x")

        bottom = ctk.CTkFrame(card, fg_color="transparent")
        bottom.pack(fill="x", padx=14, pady=(8, 12))

        status_lbl = ctk.CTkLabel(
            bottom,
            text=status_text(device),
            font=ctk.CTkFont(size=12),
            text_color=COLORS["muted"],
            anchor="e",
        )
        status_lbl.pack(side="right", fill="x", expand=True)

        ctk.CTkButton(
            bottom,
            text="حذف",
            width=64,
            height=28,
            fg_color="#5c2b2b",
            hover_color="#7a3535",
            command=lambda did=device.id: self._open_safe(lambda: self._delete_device(did)),
        ).pack(side="left")
        ctk.CTkButton(
            bottom,
            text="ویرایش",
            width=64,
            height=28,
            fg_color=COLORS["panel2"],
            command=lambda did=device.id: self._open_safe(lambda: self._edit_device(did)),
        ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            bottom,
            text="Config",
            width=70,
            height=28,
            fg_color="#6b4f1d",
            hover_color="#8a6524",
            command=lambda did=device.id: self._open_safe(
                lambda: self._open_config_browser(did)
            ),
        ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            bottom,
            text="جزئیات",
            width=78,
            height=28,
            fg_color=COLORS["accent"],
            command=lambda did=device.id: self._open_safe(lambda: self._open_detail(did)),
        ).pack(side="left", padx=(6, 0))

        card._status_lbl = status_lbl  # type: ignore[attr-defined]
        card._type_badge = type_badge  # type: ignore[attr-defined]
        card._meta_lbl = meta_lbl  # type: ignore[attr-defined]
        return card

    def _open_safe(self, fn) -> None:
        if not self._alive:
            return
        try:
            fn()
        except Exception:
            pass

    def _render_ranking(self) -> None:
        if not self._alive:
            return
        for child in self.rank_scroll.winfo_children():
            child.destroy()
        self._rank_rows.clear()

        with self._lock:
            ranked = sorted(
                self.app_data.devices,
                key=lambda d: (
                    d.temperature is None,
                    -(d.temperature or -999),
                ),
            )
            ranked = list(ranked)

        if not ranked:
            ctk.CTkLabel(
                self.rank_scroll,
                text="لیست خالی است",
                text_color=COLORS["muted"],
            ).pack(pady=20)
            return

        for rank, device in enumerate(ranked, start=1):
            row = self._make_rank_row(device, rank)
            row.pack(fill="x", pady=1)
            self._rank_rows[device.id] = row
        self._last_order_key = tuple(d.id for d in ranked)

    def _metric_chip_mini(
        self,
        parent,
        title: str,
        value: str,
        color: str,
        *,
        width: int = 58,
    ) -> tuple[ctk.CTkFrame, ctk.CTkLabel]:
        """Compact chip — keeps many devices visible in the strip."""
        box = ctk.CTkFrame(
            parent,
            fg_color=COLORS["panel2"],
            corner_radius=6,
            border_width=1,
            border_color=color,
            width=width,
            height=36,
        )
        box.pack_propagate(False)
        val_lbl = ctk.CTkLabel(
            box,
            text=value,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=color,
        )
        val_lbl.pack(pady=(2, 0))
        ctk.CTkLabel(
            box,
            text=title,
            font=ctk.CTkFont(size=8),
            text_color=COLORS["muted"],
        ).pack()
        return box, val_lbl

    def _make_rank_row(self, device: Device, rank: int) -> ctk.CTkFrame:
        online = device.status == "online"
        color = temp_color(device.temperature, online)
        bg = temp_bg(device.temperature, online)
        t_val, c_val, r_val, p_val = self._fmt_metrics(device)
        dyn = device.dynamic_info or {}
        cpu_v = dyn.get("cpu_percent")
        ram_v = dyn.get("memory_percent")
        poe_w = dyn.get("poe_used_w")
        poe_cap = dyn.get("poe_capacity_w")
        poe_v = dyn.get("poe_percent")
        cpu_c = pct_color(None if cpu_v is None else float(cpu_v))
        ram_c = pct_color(None if ram_v is None else float(ram_v))
        if poe_w is not None and poe_cap:
            poe_c = pct_color(100.0 * float(poe_w) / float(poe_cap))
        else:
            poe_c = pct_color(None if poe_v is None else float(poe_v))

        row = ctk.CTkFrame(
            self.rank_scroll,
            fg_color=bg,
            corner_radius=8,
            border_width=1,
            border_color=color,
        )

        body = ctk.CTkFrame(row, fg_color="transparent")
        body.pack(fill="x", padx=6, pady=4)

        # Compact row: type · rank · TEMP · CPU · RAM · PoE
        head = ctk.CTkFrame(body, fg_color="transparent")
        head.pack(fill="x")

        type_code, type_color = TYPE_BADGE.get(
            device.device_type, TYPE_BADGE["unknown"]
        )
        ctk.CTkLabel(
            head,
            text=type_code,
            width=26,
            height=36,
            corner_radius=5,
            fg_color=type_color,
            text_color="#041018",
            font=ctk.CTkFont(size=10, weight="bold"),
        ).pack(side="left")
        rank_lbl = ctk.CTkLabel(
            head,
            text=f"#{rank}",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLORS["muted"],
            width=28,
        )
        rank_lbl.pack(side="left", padx=(3, 4))

        chips = ctk.CTkFrame(head, fg_color="transparent")
        chips.pack(side="left", fill="x", expand=True)

        temp_box, temp_val = self._metric_chip_mini(chips, "TEMP", t_val, color, width=54)
        temp_box.pack(side="left", padx=(0, 3))
        cpu_box, cpu_val = self._metric_chip_mini(chips, "CPU", c_val, cpu_c, width=54)
        cpu_box.pack(side="left", padx=(0, 3))
        ram_box, ram_val = self._metric_chip_mini(chips, "RAM", r_val, ram_c, width=54)
        ram_box.pack(side="left", padx=(0, 3))
        # PoE wider so watts stay readable without bloating the whole card
        poe_box, poe_val = self._metric_chip_mini(chips, "PoE", p_val, poe_c, width=78)
        poe_box.pack(side="left")

        name_lbl = ctk.CTkLabel(
            body,
            text=f"{device.name}  ·  {device.host}",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=COLORS["text"],
            anchor="e",
        )
        name_lbl.pack(fill="x", pady=(2, 0))

        row._device_id = device.id  # type: ignore[attr-defined]
        row._rank_lbl = rank_lbl  # type: ignore[attr-defined]
        row._name_lbl = name_lbl  # type: ignore[attr-defined]
        row._temp_box = temp_box  # type: ignore[attr-defined]
        row._temp_val = temp_val  # type: ignore[attr-defined]
        row._cpu_box = cpu_box  # type: ignore[attr-defined]
        row._cpu_val = cpu_val  # type: ignore[attr-defined]
        row._ram_box = ram_box  # type: ignore[attr-defined]
        row._ram_val = ram_val  # type: ignore[attr-defined]
        row._poe_box = poe_box  # type: ignore[attr-defined]
        row._poe_val = poe_val  # type: ignore[attr-defined]
        row.bind("<Enter>", lambda e, did=device.id: self._on_rank_enter(e, did))
        row.bind("<Leave>", lambda _e: self._on_rank_leave())
        row.bind(
            "<Double-Button-1>",
            lambda _e, did=device.id: self._open_safe(lambda: self._open_detail(did)),
        )
        for child in (body, head, chips, name_lbl, rank_lbl):
            child.bind(
                "<Double-Button-1>",
                lambda _e, did=device.id: self._open_safe(
                    lambda: self._open_detail(did)
                ),
            )
        return row

    def _update_rank_row_live(self, device: Device, rank: int) -> None:
        row = self._rank_rows.get(device.id)
        if not row:
            return
        try:
            if not row.winfo_exists():
                return
        except Exception:
            return

        online = device.status == "online"
        color = temp_color(device.temperature, online)
        bg = temp_bg(device.temperature, online)
        t_val, c_val, r_val, p_val = self._fmt_metrics(device)
        dyn = device.dynamic_info or {}
        cpu_v = dyn.get("cpu_percent")
        ram_v = dyn.get("memory_percent")
        poe_w = dyn.get("poe_used_w")
        poe_cap = dyn.get("poe_capacity_w")
        poe_v = dyn.get("poe_percent")
        cpu_c = pct_color(None if cpu_v is None else float(cpu_v))
        ram_c = pct_color(None if ram_v is None else float(ram_v))
        if poe_w is not None and poe_cap:
            poe_c = pct_color(100.0 * float(poe_w) / float(poe_cap))
        else:
            poe_c = pct_color(None if poe_v is None else float(poe_v))
        try:
            row.configure(fg_color=bg, border_color=color)
            row._rank_lbl.configure(text=f"#{rank}")  # type: ignore[attr-defined]
            row._name_lbl.configure(text=f"{device.name}  ·  {device.host}")  # type: ignore[attr-defined]
            row._temp_box.configure(border_color=color)  # type: ignore[attr-defined]
            row._temp_val.configure(text=t_val, text_color=color)  # type: ignore[attr-defined]
            row._cpu_box.configure(border_color=cpu_c)  # type: ignore[attr-defined]
            row._cpu_val.configure(text=c_val, text_color=cpu_c)  # type: ignore[attr-defined]
            row._ram_box.configure(border_color=ram_c)  # type: ignore[attr-defined]
            row._ram_val.configure(text=r_val, text_color=ram_c)  # type: ignore[attr-defined]
            row._poe_box.configure(border_color=poe_c)  # type: ignore[attr-defined]
            row._poe_val.configure(text=p_val, text_color=poe_c)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _on_rank_enter(self, event, device_id: str) -> None:
        if not self._alive or not self._hover:
            return
        with self._lock:
            device = next(
                (d for d in self.app_data.devices if d.id == device_id), None
            )
        if not device:
            return
        try:
            self._hover.show_for(device, event.x_root, event.y_root)
        except Exception:
            pass

    def _on_rank_leave(self) -> None:
        if self._hover and self._alive:
            try:
                self._hover.hide()
            except Exception:
                pass

    def _update_card_live(self, device: Device) -> None:
        """Left cards: only text labels — never rebuild widgets."""
        card = self._device_cards.get(device.id)
        if not card:
            return
        try:
            if not card.winfo_exists():
                return
        except Exception:
            return
        type_code, type_color = TYPE_BADGE.get(
            device.device_type, TYPE_BADGE["unknown"]
        )
        type_fa = TYPE_LABELS.get(device.device_type, "نامشخص")
        model = (device.static_info or {}).get("model") or ""
        meta = f"{type_fa}  ·  {device.host}"
        if model:
            meta += f"  ·  {model}"
        if device.simulate:
            meta += "  ·  شبیه‌سازی"
        try:
            card._status_lbl.configure(text=status_text(device))  # type: ignore[attr-defined]
            card._type_badge.configure(text=type_code, fg_color=type_color)  # type: ignore[attr-defined]
            card._meta_lbl.configure(text=meta)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _force_poll(self) -> None:
        self._force_inventory = True
        threading.Thread(target=self._poll_temperatures, daemon=True).start()
        threading.Thread(target=self._poll_inventory, daemon=True).start()

    def _temp_poll_loop(self) -> None:
        while not self._stop.is_set():
            self._poll_temperatures()
            for _ in range(max(15, self.app_data.temp_poll_seconds) * 10):
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    def _inventory_poll_loop(self) -> None:
        # Stagger: first inventory shortly after start, then every 30s check due devices
        time.sleep(0.5)
        while not self._stop.is_set():
            self._poll_inventory()
            for _ in range(200):  # 20 seconds
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    def _poll_temperatures(self) -> None:
        with self._lock:
            devices = list(self.app_data.devices)
        if not devices:
            return

        def work(device: Device):
            if device.simulate:
                import math
                import random

                wave = 8 * math.sin(time.time() / 18 + hash(device.id) % 7)
                noise = random.uniform(-1.2, 1.2)
                temp = max(20.0, device.sim_base + wave + noise)
                return device.id, {
                    "temperature": round(temp, 1),
                    "status": "online",
                    "last_error": "",
                }
            try:
                reading: TempReading = read_cisco_temperature(
                    device.host, device.community, device.port, timeout=2.0
                )
                if reading.temperature is not None:
                    return device.id, {
                        "temperature": reading.temperature,
                        "status": "online",
                        "last_error": "",
                    }
                if reading.reachable:
                    return device.id, {
                        "temperature": None,
                        "status": "nosensor",
                        "last_error": reading.error or "سنسور دما یافت نشد",
                    }
                return device.id, {
                    "temperature": None,
                    "status": "offline",
                    "last_error": reading.error or "SNMP قطع است",
                }
            except Exception as exc:
                return device.id, {
                    "temperature": None,
                    "status": "offline",
                    "last_error": str(exc),
                }

        workers = min(8, max(1, len(devices)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, d) for d in devices]
            for fut in as_completed(futures):
                if self._stop.is_set():
                    break
                try:
                    did, payload = fut.result()
                except Exception:
                    continue
                with self._lock:
                    device = next(
                        (d for d in self.app_data.devices if d.id == did), None
                    )
                    if not device:
                        continue
                    device.temperature = payload["temperature"]
                    device.status = payload["status"]
                    device.last_error = payload["last_error"]
        self._poll_tick += 1

    def _poll_inventory(self) -> None:
        if self._inventory_busy:
            return
        self._inventory_busy = True
        try:
            with self._lock:
                devices = list(self.app_data.devices)
                force = self._force_inventory
                self._force_inventory = False
                dyn_every = self.app_data.dynamic_poll_seconds
            now = time.time()
            due = []
            for device in devices:
                need_static = force or not device.static_fetched_at
                need_dynamic = force or (
                    not device.dynamic_fetched_at
                    or (now - device.dynamic_fetched_at) >= dyn_every
                )
                if need_static or need_dynamic:
                    due.append((device, need_static, need_dynamic))

            if not due:
                return

            def work(item):
                device, need_static, need_dynamic = item
                result = {
                    "id": device.id,
                    "static": None,
                    "device_type": None,
                    "dynamic": None,
                    "simulate_static": None,
                }
                if device.simulate:
                    import math
                    import random

                    wave = 8 * math.sin(time.time() / 18 + hash(device.id) % 7)
                    noise = random.uniform(-1.2, 1.2)
                    if need_static:
                        result["simulate_static"] = {
                            "device_type": "switch",
                            "static_info": {
                                "sys_name": device.name,
                                "model": "Simulated Switch",
                                "serial": "SIM-" + device.host.replace(".", ""),
                                "ios_version": "17.9.SIM",
                                "device_type_label": "سوییچ",
                                "if_number": 24,
                                "sys_location": "Lab",
                            },
                        }
                    if need_dynamic:
                        result["dynamic"] = {
                            "uptime": "3d 04:12:00",
                            "cpu_percent": int(20 + abs(wave)),
                            "memory_percent": round(45 + noise, 1),
                            "poe_percent": round(30 + abs(noise) * 3, 1),
                            "poe_used_w": round(120 + abs(noise) * 10, 1),
                            "poe_capacity_w": 740.0,
                            "if_number": 24,
                        }
                    return result

                if need_static:
                    try:
                        inv = fetch_static(
                            device.host, device.community, device.port, timeout=2.5
                        )
                        if inv.ok:
                            result["static"] = inv.static
                            result["device_type"] = inv.device_type
                    except Exception:
                        pass
                if need_dynamic:
                    try:
                        dyn = fetch_dynamic(
                            device.host, device.community, device.port, timeout=4.0
                        )
                        if dyn.ok:
                            result["dynamic"] = dyn.dynamic
                    except Exception:
                        pass
                return result

            workers = min(4, max(1, len(due)))
            static_changed = False
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(work, item) for item in due]
                for fut in as_completed(futures):
                    if self._stop.is_set():
                        break
                    try:
                        result = fut.result()
                    except Exception:
                        continue
                    with self._lock:
                        device = next(
                            (
                                d
                                for d in self.app_data.devices
                                if d.id == result["id"]
                            ),
                            None,
                        )
                        if not device:
                            continue
                        if result.get("simulate_static"):
                            device.device_type = result["simulate_static"]["device_type"]
                            device.static_info = result["simulate_static"]["static_info"]
                            device.static_fetched_at = time.time()
                            static_changed = True
                        if result.get("static") is not None:
                            device.static_info = result["static"]
                            device.device_type = result.get("device_type") or device.device_type
                            device.static_fetched_at = time.time()
                            static_changed = True
                        if result.get("dynamic") is not None:
                            device.dynamic_info = result["dynamic"]
                            device.dynamic_fetched_at = time.time()
            # Dynamic metrics are in-memory only; avoid rewriting devices.json every poll
            if static_changed:
                with self._lock:
                    save_state(self.app_data)
        finally:
            self._inventory_busy = False

    def _schedule_refresh(self) -> None:
        if not self._alive or self._stop.is_set():
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        with self._lock:
            devices = list(self.app_data.devices)
            online = sum(1 for d in devices if d.status in ("online", "nosensor"))
            total = len(devices)
            ranked = sorted(
                devices,
                key=lambda d: (d.temperature is None, -(d.temperature or -999)),
            )
            order_key = tuple(d.id for d in ranked)

        for device in devices:
            self._update_card_live(device)

        # Rebuild ranking only when order changes (new device / reorder)
        if order_key != self._last_order_key or set(self._rank_rows) != set(order_key):
            self._last_order_key = order_key
            self._render_ranking()
        else:
            for rank, device in enumerate(ranked, start=1):
                self._update_rank_row_live(device, rank)

        try:
            self.status_lbl.configure(
                text=f"آنلاین: {online}/{total}  ·  پایش دما #{self._poll_tick}"
            )
        except Exception:
            return

        if self._alive and not self._stop.is_set():
            self._refresh_after_id = self.after(1500, self._schedule_refresh)

    def _on_close(self) -> None:
        self._alive = False
        self._stop.set()
        if self._refresh_after_id is not None:
            try:
                self.after_cancel(self._refresh_after_id)
            except Exception:
                pass
            self._refresh_after_id = None
        if self._hover is not None:
            try:
                self._hover.destroy()
            except Exception:
                pass
            self._hover = None
        try:
            save_state(self.app_data)
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


def main() -> None:
    app = TempMonitorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
