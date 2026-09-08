"""Turn LibreHardwareMonitor's /data.json tree into one flat, friendly dict.

LibreHardwareMonitor publishes a tree: PC -> hardware (CPU, GPU, RAM, disks, NICs)
-> category (Temperatures, Load, Clocks, ...) -> sensor leaf with a string Value
such as "61.0 °C". Names differ between AMD/Intel/Nvidia, so selection is by
ordered preference lists with sensible fallbacks.
"""
import re

_NUM = re.compile(r"^\s*([-+]?[\d.,]+)\s*(.*?)\s*$")


def _num(value):
    """'61.0 °C' -> (61.0, '°C'); '4,850.2 MHz' -> (4850.2, 'MHz'); '61,4 °C' -> (61.4, '°C').

    LibreHardwareMonitor formats numbers in the PC's locale, so commas may be thousands
    separators or decimal points. Returns (None, '') when unparsable.
    """
    if value is None:
        return None, ""
    m = _NUM.match(str(value))
    if not m:
        return None, ""
    raw, unit = m.group(1), m.group(2)
    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        head, _, tail = raw.rpartition(",")
        thousands = len(tail) == 3 and head.replace(",", "").lstrip("+-").isdigit()
        raw = raw.replace(",", "") if thousands else raw.replace(",", ".")
    try:
        return float(raw), unit
    except ValueError:
        return None, ""


def _walk(node, hw="", icon="", cat="", out=None):
    """Flatten the tree into leaf sensors tagged with hardware, icon and category."""
    if out is None:
        out = []
    img = node.get("ImageURL", "") or ""
    text = node.get("Text", "") or ""
    if img.startswith("images_icon/"):
        hw, icon = text, img.rsplit("/", 1)[-1]
    elif img.startswith("images/") and not img.endswith("transparent.png"):
        cat = text
    children = node.get("Children") or []
    if children:
        for child in children:
            _walk(child, hw, icon, cat, out)
    elif "Value" in node:
        val, unit = _num(node.get("Value"))
        if val is not None:
            out.append({"hw": hw, "icon": icon, "cat": cat, "name": text, "value": val, "unit": unit})
    return out


def _pick(sensors, icons, cat, names=(), contains=()):
    """First sensor whose hardware icon starts with one of `icons`, in category `cat`,
    matching the first name in `names` that exists (then any name containing one of `contains`)."""
    pool = [s for s in sensors if s["cat"] == cat and any(s["icon"].startswith(i) for i in icons)]
    for want in names:
        for s in pool:
            if s["name"] == want:
                return s
    for frag in contains:
        for s in pool:
            if frag.lower() in s["name"].lower():
                return s
    return pool[0] if pool and not names and not contains else None


def _to_gb(sensor):
    if not sensor:
        return None
    if sensor["unit"].upper().startswith("MB"):
        return round(sensor["value"] / 1024, 2)
    if sensor["unit"].upper().startswith("KB"):
        return round(sensor["value"] / 1024 / 1024, 3)
    return round(sensor["value"], 2)


def _to_mbps(sensor):
    """Throughput sensor -> megabits per second."""
    if not sensor:
        return None
    u = sensor["unit"].upper()
    v = sensor["value"]
    if u.startswith("KB"):
        v = v / 1024
    elif u.startswith("B/"):
        v = v / 1024 / 1024
    elif u.startswith("GB"):
        v = v * 1024
    return round(v * 8, 2)


def _to_mhz(sensor):
    if not sensor:
        return None
    return round(sensor["value"] * 1000 if sensor["unit"].upper().startswith("GHZ") else sensor["value"])


def parse_lhm(doc):
    sensors = _walk(doc)

    cpu_icons = ("cpu", "amd", "intel")
    cpu_hw = next((s["hw"] for s in sensors if s["icon"].startswith("cpu")), "")
    cpu = {
        "name": cpu_hw,
        "load": (_pick(sensors, ("cpu",), "Load", ("CPU Total",), ("total",)) or {}).get("value"),
        "temp": (_pick(sensors, ("cpu",), "Temperatures",
                       ("Core (Tctl/Tdie)", "CPU Package", "Core Average", "Core Max", "CPU Die (average)"),
                       ("tctl", "package", "average", "core")) or {}).get("value"),
        "clock": _to_mhz(_pick(sensors, ("cpu",), "Clocks", ("CPU Core #1", "Core #1"), ("core",))),
        "power": (_pick(sensors, ("cpu",), "Powers", ("Package", "CPU Package"), ("package", "cpu")) or {}).get("value"),
    }

    gpu_icons = ("nvidia", "ati", "amd")
    gpu_hw = next((s["hw"] for s in sensors if s["icon"].startswith(gpu_icons)), "")
    if not gpu_hw:
        gpu_icons = ("intel",)
        gpu_hw = next((s["hw"] for s in sensors if s["icon"].startswith("intel")), "")
    vram_used = _pick(sensors, gpu_icons, "Data", ("GPU Memory Used", "D3D Dedicated Memory Used"), ("memory used",))
    vram_total = _pick(sensors, gpu_icons, "Data", ("GPU Memory Total",), ("memory total",))
    gpu = {
        "name": gpu_hw,
        "load": (_pick(sensors, gpu_icons, "Load", ("GPU Core", "D3D 3D"), ("core", "3d")) or {}).get("value"),
        "temp": (_pick(sensors, gpu_icons, "Temperatures", ("GPU Core",), ("core", "gpu")) or {}).get("value"),
        "hotspot": (_pick(sensors, gpu_icons, "Temperatures", ("GPU Hot Spot",), ("hot",)) or {}).get("value"),
        "vram_used_gb": _to_gb(vram_used),
        "vram_total_gb": _to_gb(vram_total),
        "fan_rpm": (_pick(sensors, gpu_icons, "Fans", ("GPU Fan", "GPU Fan 1"), ("fan",)) or {}).get("value"),
        "fan_pct": (_pick(sensors, gpu_icons, "Controls", ("GPU Fan", "GPU Fan 1"), ("fan",)) or {}).get("value"),
        "power": (_pick(sensors, gpu_icons, "Powers", ("GPU Package", "GPU Power"), ("package", "power", "gpu")) or {}).get("value"),
        "clock": _to_mhz(_pick(sensors, gpu_icons, "Clocks", ("GPU Core",), ("core",))),
    }

    ram_used = _pick(sensors, ("ram",), "Data", ("Memory Used",), ("used",))
    ram_avail = _pick(sensors, ("ram",), "Data", ("Memory Available",), ("available",))
    used_gb, avail_gb = _to_gb(ram_used), _to_gb(ram_avail)
    ram = {
        "used_gb": used_gb,
        "total_gb": round(used_gb + avail_gb, 1) if used_gb is not None and avail_gb is not None else None,
        "load": (_pick(sensors, ("ram",), "Load", ("Memory",), ("memory",)) or {}).get("value"),
    }

    storage = []
    for hw in dict.fromkeys(s["hw"] for s in sensors if s["icon"].startswith(("hdd", "ssd", "nvme"))):
        t = next((s for s in sensors if s["hw"] == hw and s["cat"] == "Temperatures"), None)
        if t:
            storage.append({"name": hw, "temp": t["value"]})

    best_net = None
    for hw in dict.fromkeys(s["hw"] for s in sensors if s["icon"].startswith("nic")):
        down = next((s for s in sensors if s["hw"] == hw and s["cat"] == "Throughput" and "Download" in s["name"]), None)
        up = next((s for s in sensors if s["hw"] == hw and s["cat"] == "Throughput" and "Upload" in s["name"]), None)
        cand = {"name": hw, "down_mbps": _to_mbps(down) or 0.0, "up_mbps": _to_mbps(up) or 0.0}
        if best_net is None or cand["down_mbps"] + cand["up_mbps"] > best_net["down_mbps"] + best_net["up_mbps"]:
            best_net = cand

    fans = [{"name": s["name"], "rpm": s["value"]} for s in sensors
            if s["cat"] == "Fans" and s["icon"].startswith(("mainboard", "chip", "fan", "control"))][:4]

    return {"cpu": cpu, "gpu": gpu, "ram": ram, "storage": storage[:4], "net": best_net, "fans": fans}
