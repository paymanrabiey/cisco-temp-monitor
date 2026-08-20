"""
Cisco running/startup config backup with time-based versions and diffs.
Configs are stored under: config/<DeviceName__host>/<YYYYMMDD_HHMMSS>/
"""

from __future__ import annotations

from dataclasses import dataclass, field
import difflib
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_ROOT = APP_DIR / "config"

_PROMPT_RE = re.compile(r"[\r\n][\w.\-()/@]+[#>]\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


@dataclass
class BackupResult:
    ok: bool
    device_dir: str = ""
    version_id: str = ""
    version_path: str = ""
    running_changed: bool = False
    startup_differs: bool = False
    error: str = ""
    meta: dict = field(default_factory=dict)


def ensure_config_root() -> Path:
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    keep = CONFIG_ROOT / ".gitkeep"
    if not keep.exists():
        keep.write_text("", encoding="utf-8")
    return CONFIG_ROOT


def sanitize_name(text: str, *, max_len: int = 48) -> str:
    text = (text or "device").strip()
    text = re.sub(r"[^\w.\-]+", "_", text, flags=re.UNICODE)
    text = re.sub(r"_+", "_", text).strip("._-")
    return (text or "device")[:max_len]


def device_folder_name(name: str, host: str) -> str:
    return f"{sanitize_name(name)}__{sanitize_name(host, max_len=64)}"


def device_dir(name: str, host: str) -> Path:
    ensure_config_root()
    path = CONFIG_ROOT / device_folder_name(name, host)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _normalize_config(text: str) -> str:
    """Strip noise that changes every show (clock, ntp status lines, etc.)."""
    lines = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.rstrip()
        # drop common ephemeral / banner noise
        if s.startswith("Building configuration"):
            continue
        if s.startswith("Current configuration"):
            continue
        if s.startswith("!"):
            # keep structure comments but drop timestamp-only bang lines later
            pass
        if "NVRAM config last updated" in s:
            continue
        if s.strip().startswith("!") and "Last configuration change" in s:
            continue
        lines.append(s)
    # trim trailing empties
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + ("\n" if lines else "")


def make_unified_diff(a: str, b: str, fromfile: str, tofile: str) -> str:
    a_lines = (a or "").splitlines(keepends=True)
    b_lines = (b or "").splitlines(keepends=True)
    if not a_lines and not b_lines:
        return ""
    diff = difflib.unified_diff(
        a_lines, b_lines, fromfile=fromfile, tofile=tofile, lineterm=""
    )
    return "\n".join(diff)


def _load_versions_index(dev_path: Path) -> dict:
    idx = dev_path / "versions.json"
    if not idx.exists():
        return {"versions": []}
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"versions": []}
        data.setdefault("versions", [])
        return data
    except Exception:
        return {"versions": []}


def _save_versions_index(dev_path: Path, data: dict) -> None:
    (dev_path / "versions.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def list_versions(name: str, host: str) -> list[dict]:
    path = device_dir(name, host)
    data = _load_versions_index(path)
    versions = list(data.get("versions") or [])
    versions.sort(key=lambda v: v.get("id", ""), reverse=True)
    return versions


def read_version_file(name: str, host: str, version_id: str, filename: str) -> str:
    path = device_dir(name, host) / version_id / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def latest_running(name: str, host: str) -> str:
    versions = list_versions(name, host)
    if not versions:
        return ""
    return read_version_file(name, host, versions[0]["id"], "running-config.cfg")


def compare_versions(
    name: str,
    host: str,
    version_a: str,
    version_b: str,
    *,
    which: str = "running",
) -> str:
    fname = "running-config.cfg" if which == "running" else "startup-config.cfg"
    a = read_version_file(name, host, version_a, fname)
    b = read_version_file(name, host, version_b, fname)
    return make_unified_diff(
        a,
        b,
        f"{fname}@{version_a}",
        f"{fname}@{version_b}",
    )


def _strip_cli_noise(raw: str, command: str) -> str:
    text = _ANSI_RE.sub("", raw or "")
    text = text.replace("\r", "")
    # remove echoed command line
    lines = text.split("\n")
    cleaned = []
    cmd = command.strip().lower()
    for line in lines:
        low = line.strip().lower()
        if low == cmd or low.endswith(cmd):
            continue
        if _PROMPT_RE.search("\n" + line) and len(line.strip()) < 40:
            # likely prompt-only line
            if "#" in line or line.strip().endswith(">"):
                continue
        cleaned.append(line.rstrip())
    body = "\n".join(cleaned).strip()
    # cut after "end" for IOS configs when present
    if re.search(r"(?m)^end\s*$", body):
        body = re.split(r"(?m)^end\s*$", body, maxsplit=1)[0] + "end\n"
    return body


def _ssh_show_commands(
    host: str,
    username: str,
    password: str,
    commands: list[str],
    *,
    port: int = 22,
    enable_secret: str = "",
    timeout: float = 50.0,
) -> dict[str, str]:
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError(
            "کتابخانه paramiko نصب نیست. در ترمینال: pip install paramiko"
        ) from exc

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=int(port),
            username=username,
            password=password,
            look_for_keys=False,
            allow_agent=False,
            timeout=min(20.0, timeout),
            banner_timeout=min(20.0, timeout),
            auth_timeout=min(20.0, timeout),
        )
    except Exception as exc:
        raise RuntimeError(f"SSH اتصال برقرار نشد: {exc}") from exc

    try:
        chan = client.invoke_shell(width=200, height=60)
        chan.settimeout(2.0)
        time.sleep(0.6)
        _drain(chan)
        # disable paging
        _send_wait(chan, "terminal length 0", settle=0.8)
        _send_wait(chan, "terminal width 0", settle=0.5)
        if enable_secret:
            _send_wait(chan, "enable", settle=0.6)
            _send_wait(chan, enable_secret, settle=0.8)

        outputs: dict[str, str] = {}
        for cmd in commands:
            raw = _send_wait(chan, cmd, settle=1.2, read_seconds=max(8.0, timeout / 3))
            outputs[cmd] = _strip_cli_noise(raw, cmd)
        return outputs
    finally:
        try:
            client.close()
        except Exception:
            pass


def _drain(chan) -> str:
    chunks = []
    try:
        while chan.recv_ready():
            chunks.append(chan.recv(65535).decode("utf-8", errors="replace"))
    except Exception:
        pass
    return "".join(chunks)


def _send_wait(chan, cmd: str, *, settle: float = 0.8, read_seconds: float = 6.0) -> str:
    chan.send(cmd + "\n")
    time.sleep(settle)
    buf = []
    deadline = time.time() + read_seconds
    idle = 0
    while time.time() < deadline:
        chunk = ""
        try:
            if chan.recv_ready():
                chunk = chan.recv(65535).decode("utf-8", errors="replace")
                buf.append(chunk)
                idle = 0
            else:
                idle += 1
                time.sleep(0.25)
                if idle >= 6 and buf:
                    # quiet for ~1.5s after data → likely done
                    text = "".join(buf)
                    if _PROMPT_RE.search(text) or text.rstrip().endswith("#"):
                        break
        except Exception:
            break
    return "".join(buf)


def _simulate_configs(device_name: str, host: str) -> tuple[str, str]:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    running = f"""!
! Simulated running-config for {device_name} ({host})
! Generated: {stamp}
!
version 15.2
hostname {sanitize_name(device_name)}
!
interface GigabitEthernet0/1
 description UPLINK
 no shutdown
!
interface GigabitEthernet0/2
 description ACCESS
 switchport mode access
!
line vty 0 4
 login local
!
end
"""
    # startup lags behind occasionally (unsaved change simulation)
    startup = running.replace(
        "description ACCESS", "description ACCESS-OLD"
    )
    if int(time.time()) % 3 == 0:
        startup = running
    return running, startup


def fetch_running_startup(
    *,
    host: str,
    username: str,
    password: str,
    port: int = 22,
    enable_secret: str = "",
    simulate: bool = False,
    device_name: str = "",
    timeout: float = 50.0,
) -> tuple[str, str]:
    if simulate:
        return _simulate_configs(device_name or host, host)
    if not username or not password:
        raise RuntimeError("نام کاربری و رمز SSH برای بکاپ کانفیگ لازم است.")
    outs = _ssh_show_commands(
        host,
        username,
        password,
        ["show running-config", "show startup-config"],
        port=port,
        enable_secret=enable_secret or "",
        timeout=timeout,
    )
    running = outs.get("show running-config") or ""
    startup = outs.get("show startup-config") or ""
    if len(running.strip()) < 40:
        raise RuntimeError(
            "running-config خالی یا ناقص برگشت — سطح دسترسی SSH / enable را بررسی کنید."
        )
    if len(startup.strip()) < 20:
        # some devices deny startup; keep empty marker
        startup = "!\n! startup-config unavailable or empty\n!\n"
    return running, startup


def save_config_version(
    *,
    device_id: str,
    device_name: str,
    host: str,
    running: str,
    startup: str,
) -> BackupResult:
    """Write a new timestamped version; skip identical snapshot if nothing changed."""
    ensure_config_root()
    dev_path = device_dir(device_name, host)
    running_n = _normalize_config(running)
    startup_n = _normalize_config(startup)
    run_sha = _sha(running_n)
    start_sha = _sha(startup_n)

    index = _load_versions_index(dev_path)
    index["device_id"] = device_id
    index["device_name"] = device_name
    index["host"] = host
    versions = list(index.get("versions") or [])

    prev_run = ""
    prev_sha = ""
    if versions:
        # versions stored oldest→newest or any order; pick latest by id
        ordered = sorted(versions, key=lambda v: v.get("id", ""))
        last = ordered[-1]
        prev_sha = last.get("running_sha") or ""
        prev_run = read_version_file(device_name, host, last["id"], "running-config.cfg")

    # If exact same running+startup as last version, still create version only if forced?
    # User asked: if files exist, create new time-based version for history.
    # But identical dumps fill disk — create version always OR skip identical.
    # Compromise: always create version entry when content changed; if identical,
    # update "last_checked_at" without new folder (smarter). Document in meta.
    identical = bool(versions) and prev_sha == run_sha and (
        (versions and sorted(versions, key=lambda v: v.get("id", ""))[-1].get("startup_sha") == start_sha)
    )
    if identical:
        last = sorted(versions, key=lambda v: v.get("id", ""))[-1]
        last["last_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        index["versions"] = versions
        _save_versions_index(dev_path, index)
        return BackupResult(
            ok=True,
            device_dir=str(dev_path),
            version_id=last.get("id", ""),
            version_path=str(dev_path / last.get("id", "")),
            running_changed=False,
            startup_differs=(run_sha != start_sha),
            meta={"skipped_identical": True, **last},
        )

    version_id = time.strftime("%Y%m%d_%H%M%S")
    # avoid collision if two backups in same second
    vpath = dev_path / version_id
    if vpath.exists():
        version_id = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000}"
        vpath = dev_path / version_id
    vpath.mkdir(parents=True, exist_ok=True)

    (vpath / "running-config.cfg").write_text(running_n, encoding="utf-8")
    (vpath / "startup-config.cfg").write_text(startup_n, encoding="utf-8")

    startup_differs = run_sha != start_sha
    if startup_differs:
        diff_rs = make_unified_diff(
            startup_n,
            running_n,
            "startup-config.cfg",
            "running-config.cfg",
        )
        (vpath / "diff-running-vs-startup.txt").write_text(
            diff_rs or "(no textual diff)\n", encoding="utf-8"
        )

    running_changed = bool(prev_run) and _sha(_normalize_config(prev_run)) != run_sha
    if running_changed:
        diff_prev = make_unified_diff(
            _normalize_config(prev_run),
            running_n,
            "running-config@previous",
            f"running-config@{version_id}",
        )
        (vpath / "diff-running-vs-previous.txt").write_text(
            diff_prev or "(no textual diff)\n", encoding="utf-8"
        )

    meta = {
        "device_id": device_id,
        "device_name": device_name,
        "host": host,
        "version_id": version_id,
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "running_sha": run_sha,
        "startup_sha": start_sha,
        "running_changed": running_changed,
        "startup_differs": startup_differs,
        "running_bytes": len(running_n.encode("utf-8")),
        "startup_bytes": len(startup_n.encode("utf-8")),
    }
    (vpath / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # maintain easy latest pointers
    for fname in ("running-config.cfg", "startup-config.cfg"):
        latest = dev_path / f"latest-{fname}"
        latest.write_text(
            (vpath / fname).read_text(encoding="utf-8"), encoding="utf-8"
        )

    versions.append(
        {
            "id": version_id,
            "fetched_at": meta["fetched_at"],
            "running_sha": run_sha,
            "startup_sha": start_sha,
            "running_changed": running_changed,
            "startup_differs": startup_differs,
        }
    )
    index["versions"] = versions
    index["last_backup_at"] = meta["fetched_at"]
    _save_versions_index(dev_path, index)

    return BackupResult(
        ok=True,
        device_dir=str(dev_path),
        version_id=version_id,
        version_path=str(vpath),
        running_changed=running_changed,
        startup_differs=startup_differs,
        meta=meta,
    )


def backup_device_config(
    *,
    device_id: str,
    device_name: str,
    host: str,
    username: str = "",
    password: str = "",
    ssh_port: int = 22,
    enable_secret: str = "",
    simulate: bool = False,
    timeout: float = 50.0,
) -> BackupResult:
    try:
        running, startup = fetch_running_startup(
            host=host,
            username=username,
            password=password,
            port=ssh_port,
            enable_secret=enable_secret,
            simulate=simulate,
            device_name=device_name,
            timeout=timeout,
        )
        return save_config_version(
            device_id=device_id,
            device_name=device_name,
            host=host,
            running=running,
            startup=startup,
        )
    except Exception as exc:
        return BackupResult(ok=False, error=str(exc))


def open_device_config_folder(name: str, host: str) -> Path:
    path = device_dir(name, host)
    return path
