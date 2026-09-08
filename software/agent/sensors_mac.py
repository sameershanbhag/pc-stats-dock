"""macOS sensors for the stats panel.

Apple Silicon: `macmon pipe` (Homebrew) streams CPU/GPU load, temperatures, power, fans and
memory as one JSON object per line, no sudo needed. Intel or no macmon: a basic mode with CPU%
from `top` and memory from `vm_stat` (no temperatures). Network comes from `netstat -ib`,
disk from the filesystem. No Python packages required.
"""
import json
import shutil
import subprocess
import threading
import time


def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def chip_name():
    return _run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"]).strip() or "Mac"


def computer_name():
    return _run(["/usr/sbin/scutil", "--get", "ComputerName"]).strip() or "Mac"


class Network:
    """Bytes per second on the busiest interface, from netstat deltas."""

    def __init__(self):
        self.prev = None  # (t, {iface: (ibytes, obytes)})

    @staticmethod
    def parse_netstat(text):
        """{iface: (ibytes, obytes)} from `netstat -ib`. The Address column is sometimes
        empty, so counters are read from the end: the last 7 fields are always
        Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll."""
        table = {}
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 8:
                continue
            name = parts[0]
            if name == "lo0" or name.startswith(("awdl", "llw", "gif", "stf", "ap", "anpi")):
                continue
            try:
                ibytes, obytes = int(parts[-5]), int(parts[-2])
            except ValueError:
                continue
            table[name] = (ibytes, obytes)
        return table

    def read(self):
        table = self.parse_netstat(_run(["/usr/sbin/netstat", "-ib"]))
        now = time.time()
        result = None
        if self.prev:
            dt = max(now - self.prev[0], 0.1)
            best = None
            for name, (i, o) in table.items():
                pi, po = self.prev[1].get(name, (i, o))
                down = max(0, i - pi) / dt * 8 / 1e6
                up = max(0, o - po) / dt * 8 / 1e6
                if best is None or down + up > best["down_mbps"] + best["up_mbps"]:
                    best = {"name": name, "down_mbps": round(down, 2), "up_mbps": round(up, 2)}
            result = best
        self.prev = (now, table)
        return result


class MacmonStream:
    """Keeps the latest JSON sample from a long-running `macmon pipe`."""

    STALE_AFTER = 5.0  # seconds without a sample -> treat as offline

    def __init__(self, interval_ms=500, start=True):
        self.latest = None
        self.last_ts = 0.0
        self.proc = None
        self.failures = 0
        self.interval_ms = interval_ms
        self.path = shutil.which("macmon") or ("/opt/homebrew/bin/macmon" if shutil.which("/opt/homebrew/bin/macmon") else None)
        if self.path and start:
            threading.Thread(target=self._pump, daemon=True).start()

    @property
    def ok(self):
        return self.latest is not None and (time.time() - self.last_ts) < self.STALE_AFTER

    def _pump(self):
        while True:
            started = time.time()
            try:
                self.proc = subprocess.Popen([self.path, "pipe", "-i", str(self.interval_ms)],
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for line in self.proc.stdout:
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            self.latest = json.loads(line)
                            self.last_ts = time.time()
                        except json.JSONDecodeError:
                            pass
            except Exception:
                pass
            # macmon exited (Intel Mac, missing binary, crash): retry, backing off if it keeps dying quickly
            self.failures = self.failures + 1 if time.time() - started < 5 else 0
            time.sleep(60 if self.failures >= 3 else 3)


def basic_cpu_mem():
    """Intel / no-macmon fallback: CPU% from top, memory from vm_stat + sysctl."""
    cpu = None
    for line in _run(["/usr/bin/top", "-l", "1", "-n", "0"]).splitlines():
        if line.startswith("CPU usage"):
            try:
                idle = float(line.split(",")[2].strip().split("%")[0])
                cpu = round(100 - idle, 1)
            except (IndexError, ValueError):
                pass
            break
    pages = {}
    for line in _run(["/usr/bin/vm_stat"]).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            try:
                pages[k.strip()] = int(v.strip().rstrip("."))
            except ValueError:
                pass
    page = 16384 if "16384" in _run(["/usr/bin/vm_stat"]).splitlines()[0] else 4096
    try:
        total = int(_run(["/usr/sbin/sysctl", "-n", "hw.memsize"]).strip())
    except ValueError:
        total = None
    used = None
    if pages:
        used = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0) + pages.get("Pages occupied by compressor", 0)) * page
    return cpu, used, total


class MacSensors:
    def __init__(self, poll_ms=500):
        self.chip = chip_name()
        self.stream = MacmonStream(poll_ms)
        self.net = Network()
        self._basic_t = 0
        self._basic = (None, None, None)

    @property
    def mode(self):
        return "macmon" if self.stream.ok else "basic"

    def read(self):
        m = self.stream.latest if self.stream.ok else None
        du = shutil.disk_usage("/")
        storage = [{"name": "Macintosh HD", "temp": None, "used_pct": round(du.used / du.total * 100, 1),
                    "used_gb": round(du.used / 1e9), "total_gb": round(du.total / 1e9)}]
        net = self.net.read()
        return self.from_macmon(m, storage, net) if m else self.from_basic(storage, net)

    def from_macmon(self, m, storage, net):
        if True:
            mem = m.get("memory") or {}
            temp = m.get("temp") or {}
            fans = [{"name": f.get("name", "fan"), "rpm": round(f.get("rpm", 0))} for f in (m.get("fans") or [])]
            gpu_usage = m.get("gpu_usage") or [None, None]
            return {
                "sensors": "macmon",
                "cpu": {"name": self.chip, "load": round(100 * (m.get("cpu_active_ratio") or 0), 1),
                        "temp": _r(temp.get("cpu_temp_avg")), "clock": m.get("pcpu_freq_mhz"),
                        "power": _r(m.get("cpu_power"))},
                "gpu": {"name": self.chip + " GPU", "load": round(100 * (m.get("gpu_active_ratio") or 0), 1),
                        "temp": _r(temp.get("gpu_temp_avg")), "hotspot": None,
                        "vram_used_gb": None, "vram_total_gb": None,
                        "fan_rpm": fans[0]["rpm"] if fans else None, "fan_pct": None,
                        "power": _r(m.get("gpu_power")), "clock": gpu_usage[0] if gpu_usage else m.get("gpu_freq_mhz")},
                "ram": {"used_gb": round((mem.get("ram_usage") or 0) / 2**30, 1),
                        "total_gb": round((mem.get("ram_total") or 0) / 2**30, 1),
                        "load": round(100 * (mem.get("ram_usage") or 0) / max(mem.get("ram_total") or 1, 1), 1),
                        "swap_used_gb": round((mem.get("swap_usage") or 0) / 2**30, 2)},
                "sys_power": _r(m.get("sys_power")),
                "ane_power": _r(m.get("ane_power")),
                "storage": storage, "net": net, "fans": fans,
            }
    def from_basic(self, storage, net):
        # refreshed every 2 s (top takes about a second)
        if time.time() - self._basic_t > 2:
            self._basic = basic_cpu_mem()
            self._basic_t = time.time()
        cpu, used, total = self._basic
        return {
            "sensors": "basic",
            "cpu": {"name": self.chip, "load": cpu, "temp": None, "clock": None, "power": None},
            "gpu": {"name": None, "load": None, "temp": None, "hotspot": None, "vram_used_gb": None, "vram_total_gb": None,
                    "fan_rpm": None, "fan_pct": None, "power": None, "clock": None},
            "ram": {"used_gb": round(used / 2**30, 1) if used else None, "total_gb": round(total / 2**30, 1) if total else None,
                    "load": round(100 * used / total, 1) if used and total else None},
            "sys_power": None, "storage": storage, "net": net, "fans": [],
        }


def _r(v, nd=1):
    return None if v is None else round(v, nd)
