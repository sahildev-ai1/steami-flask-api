"""
SysWatch Router — plugs into STEAMI's FastAPI app
Mount in main.py:
    from routers.syswatch import router as syswatch_router
    app.include_router(syswatch_router)
"""

import os
import time
import socket
import datetime
import platform
import subprocess
from typing import List, Dict, Any

import psutil
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/api/syswatch", tags=["SysWatch"])

# ─── Helpers ──────────────────────────────────────────────────────────────────

def bytes_to_human(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def uptime_human() -> str:
    boot = psutil.boot_time()
    delta = int(time.time() - boot)
    d, r = divmod(delta, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    return " ".join(parts) or "< 1m"

# ─── Routes ───────────────────────────────────────────────────────────────────

@router.get("/overview")
def get_overview() -> Dict[str, Any]:
    """Complete system snapshot — called on dashboard load."""
    cpu_freq = psutil.cpu_freq()
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net_io = psutil.net_io_counters()
    boot_time = datetime.datetime.fromtimestamp(psutil.boot_time())

    return {
        "hostname": socket.gethostname(),
        "platform": platform.system(),
        "os": f"{platform.system()} {platform.release()}",
        "kernel": platform.version().split()[0] if platform.version() else "unknown",
        "architecture": platform.machine(),
        "boot_time": boot_time.strftime("%Y-%m-%d %H:%M"),
        "uptime": uptime_human(),
        "cpu": {
            "percent": psutil.cpu_percent(interval=0.3),
            "count_logical": psutil.cpu_count(logical=True),
            "count_physical": psutil.cpu_count(logical=False),
            "freq_mhz": round(cpu_freq.current) if cpu_freq else None,
            "per_core": psutil.cpu_percent(percpu=True, interval=0.1),
        },
        "memory": {
            "percent": mem.percent,
            "used": bytes_to_human(mem.used),
            "total": bytes_to_human(mem.total),
            "available": bytes_to_human(mem.available),
            "cached": bytes_to_human(getattr(mem, "cached", 0)),
        },
        "disk": {
            "percent": disk.percent,
            "used": bytes_to_human(disk.used),
            "total": bytes_to_human(disk.total),
            "free": bytes_to_human(disk.free),
        },
        "network": {
            "bytes_sent": bytes_to_human(net_io.bytes_sent),
            "bytes_recv": bytes_to_human(net_io.bytes_recv),
            "packets_sent": net_io.packets_sent,
            "packets_recv": net_io.packets_recv,
        },
    }


@router.get("/cpu")
def get_cpu():
    """Live CPU sample — polled every 2s by dashboard."""
    cpu_freq = psutil.cpu_freq()
    return {
        "percent": psutil.cpu_percent(interval=0.2),
        "per_core": psutil.cpu_percent(percpu=True, interval=0.2),
        "freq_mhz": round(cpu_freq.current) if cpu_freq else 0,
        "load_avg": [round(x, 2) for x in os.getloadavg()]
            if hasattr(os, "getloadavg") else [0, 0, 0],
    }


@router.get("/memory")
def get_memory():
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "percent": mem.percent,
        "used_bytes": mem.used,
        "total_bytes": mem.total,
        "used": bytes_to_human(mem.used),
        "total": bytes_to_human(mem.total),
        "available": bytes_to_human(mem.available),
        "swap_percent": swap.percent,
        "swap_used": bytes_to_human(swap.used),
        "swap_total": bytes_to_human(swap.total),
    }


@router.get("/disk")
def get_disk():
    partitions = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            partitions.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "percent": usage.percent,
                "used": bytes_to_human(usage.used),
                "total": bytes_to_human(usage.total),
                "free": bytes_to_human(usage.free),
            })
        except PermissionError:
            pass
    io = psutil.disk_io_counters()
    return {
        "partitions": partitions,
        "read_bytes": bytes_to_human(io.read_bytes) if io else "N/A",
        "write_bytes": bytes_to_human(io.write_bytes) if io else "N/A",
        "read_count": io.read_count if io else 0,
        "write_count": io.write_count if io else 0,
    }


@router.get("/network")
def get_network():
    ifaces = []
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    io_per = psutil.net_io_counters(pernic=True)

    for name, stat in stats.items():
        addr_list = addrs.get(name, [])
        ipv4 = next((a.address for a in addr_list
                     if a.family == socket.AF_INET), "—")
        io = io_per.get(name)
        ifaces.append({
            "name": name,
            "is_up": stat.isup,
            "speed_mbps": stat.speed,
            "ipv4": ipv4,
            "bytes_sent": bytes_to_human(io.bytes_sent) if io else "—",
            "bytes_recv": bytes_to_human(io.bytes_recv) if io else "—",
            "packets_sent": io.packets_sent if io else 0,
            "packets_recv": io.packets_recv if io else 0,
            "errors_in": io.errin if io else 0,
            "errors_out": io.errout if io else 0,
        })
    return {"interfaces": ifaces}


@router.get("/processes")
def get_processes(limit: int = 12):
    """Top processes by CPU usage."""
    procs = []
    for proc in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_info", "status", "username", "cmdline"]
    ):
        try:
            info = proc.info
            mem_mb = (info["memory_info"].rss / 1024 / 1024) if info["memory_info"] else 0
            cmd = " ".join(info["cmdline"][:3]) if info["cmdline"] else info["name"]
            procs.append({
                "pid": info["pid"],
                "name": info["name"],
                "cmd": cmd[:48],
                "cpu_percent": info["cpu_percent"] or 0.0,
                "mem_mb": round(mem_mb, 1),
                "status": info["status"],
                "user": info["username"] or "—",
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    procs.sort(key=lambda x: x["cpu_percent"], reverse=True)
    return {"processes": procs[:limit], "total": len(procs)}


@router.get("/users")
def get_users():
    users = []
    for u in psutil.users():
        login_time = datetime.datetime.fromtimestamp(u.started)
        users.append({
            "name": u.name,
            "terminal": u.terminal or "—",
            "host": u.host or "local",
            "started": login_time.strftime("%H:%M"),
            "started_full": login_time.strftime("%Y-%m-%d %H:%M"),
        })
    return {"users": users, "count": len(users)}


@router.get("/logs")
def get_logs(lines: int = 30):
    """
    Reads from /var/log/syslog (Linux) or system log.
    Falls back to a synthetic log if not accessible.
    """
    log_paths = ["/var/log/syslog", "/var/log/messages", "/var/log/system.log"]
    entries = []

    for path in log_paths:
        if os.path.exists(path):
            try:
                result = subprocess.run(
                    ["tail", "-n", str(lines), path],
                    capture_output=True, text=True, timeout=3
                )
                raw_lines = result.stdout.strip().split("\n")
                for line in reversed(raw_lines):
                    if not line.strip():
                        continue
                    level = "INFO"
                    if any(w in line.lower() for w in ["error", "err", "failed", "failure"]):
                        level = "ERROR"
                    elif any(w in line.lower() for w in ["warn", "warning"]):
                        level = "WARN"
                    elif any(w in line.lower() for w in ["started", "success", "ok", "ready"]):
                        level = "OK"
                    entries.append({"time": line[:15], "level": level, "msg": line[16:80]})
                return {"entries": entries[:lines], "source": path}
            except Exception:
                continue

    # Synthetic fallback (when running without syslog access)
    now = datetime.datetime.now()
    synthetic = [
        ("OK",    "cron[1204]: health-check.sh — all services running"),
        ("INFO",  f"sshd[998]: Accepted key for {os.getlogin()} from 127.0.0.1"),
        ("WARN",  "kernel: Memory usage crossed 65% threshold"),
        ("OK",    "nginx[1024]: 200 OK — 214 requests served"),
        ("INFO",  "systemd[1]: Starting periodic filesystem check"),
        ("OK",    "backup[3310]: /home snapshot completed — 2.4 GB"),
        ("INFO",  "cron[1204]: Triggered log-rotate.sh"),
        ("WARN",  "disk[44]: Read latency spike on /dev/sda — 320ms"),
        ("OK",    "postgres[2201]: Checkpoint completed — WAL size 128 MB"),
        ("INFO",  "sshd[998]: New session opened for researcher"),
    ]
    for i, (level, msg) in enumerate(synthetic):
        t = (now - datetime.timedelta(minutes=i * 4)).strftime("%b %d %H:%M:%S")
        entries.append({"time": t, "level": level, "msg": msg})

    return {"entries": entries, "source": "synthetic"}


@router.get("/health")
def health_check():
    """Quick health ping."""
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    status = "healthy"
    issues = []

    if psutil.cpu_percent(interval=0.2) > 90:
        issues.append("CPU critical")
        status = "warning"
    if mem.percent > 90:
        issues.append("Memory critical")
        status = "critical"
    if disk.percent > 95:
        issues.append("Disk full")
        status = "critical"

    return {
        "status": status,
        "issues": issues,
        "timestamp": datetime.datetime.now().isoformat(),
    }