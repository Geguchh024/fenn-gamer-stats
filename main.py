"""Gamer Stats: GPU temperature, load, power, clock, fan and VRAM, CPU
temperature and load, RAM, network speed and ping (with jitter and packet
loss), drawn as gauges and sparklines. Fullscreen games are noticed on
their own: each session is timed and keeps its peak temperatures and ping.

GPU numbers come from nvidia-smi when it's installed (it ships with the
Nvidia driver), otherwise from LibreHardwareMonitor's WMI namespace while
that app is running. CPU temperature comes from LibreHardwareMonitor too, or
falls back to the ACPI thermal zone. The page says plainly when a source is
missing. Sampling runs on a worker thread: every second while the page is
open, every 2 seconds otherwise. On battery with no game running (and the
page closed), the GPU isn't asked at all, so a laptop's GPU can sleep."""

import datetime as dt
import json
import sys
import threading
import time
from collections import deque

import fenn

if sys.platform == "win32":
    import probes
else:  # lets the checker run elsewhere
    probes = None

HISTORY = 90
DEFAULT_HOST = "1.1.1.1"
GAME_AFTER = 20     # seconds in the foreground before a fullscreen app counts as a game
GAME_GRACE = 45     # alt-tab this long before the session ends
METRICS = ["gpu_temp", "gpu_load", "gpu_power", "gpu_clock", "gpu_fan", "vram", "cpu_temp", "cpu_load",
           "ram", "net_down", "net_up", "ping"]

app = fenn.Plugin()
settings = {"host": DEFAULT_HOST, "saver": True, "gpu_alert": 87, "cpu_alert": 95}
settings.update(app.load("settings", {}))
sessions = app.load("sessions", [])     # finished game sessions, newest last
hist = {m: deque(maxlen=HISTORY) for m in METRICS}
cur = {m: None for m in METRICS}
pings = deque(maxlen=60)                # (ok, ms) for jitter and loss
info = {"gpu_name": "", "gpu_src": None, "cpu_src": None, "fan_unit": None, "vram_total": None,
        "ram_total": None, "ping_error": "", "lhm": None, "started": False, "power": None,
        "paused_gpu": False, "game": None}
ui = {"host_n": 0, "page_w": 608, "alert": None, "seen": None}
game = {"live": None}                   # the session being played
shared = {"interval": 2.0, "host": settings["host"], "stop": False, "page": False, "saver": settings["saver"]}


def post(msg):
    app._inbox.put(msg)


# Sampling (worker thread) ---------------------------------------------------------

def sampler():
    nv = probes.Nvidia()
    helper = probes.SensorHelper()
    cpu = probes.CpuLoad()
    net = probes.Network()
    try:
        pinger = probes.Pinger()
    except OSError:
        pinger = None
    cpu.read()
    net.read()
    while not shared["stop"]:
        t0 = time.monotonic()
        s = {"type": "sample"}
        s["power"] = probes.power()
        s["game"] = probes.foreground_game()
        on_battery = bool(s["power"]) and not s["power"]["ac"]
        # On battery, nvidia-smi would keep a sleeping laptop GPU awake.
        skip_gpu = shared["saver"] and on_battery and not s["game"] and not shared["page"]
        s["paused_gpu"] = skip_gpu
        g = None if skip_gpu else nv.read()
        s["gpu_src"] = "nvidia-smi" if g else None
        sens = helper.read() or {}
        s["lhm"] = sens.get("lhm")
        if not g and sens.get("gpu") and not skip_gpu:
            g = sens["gpu"]
            s["gpu_src"] = "LibreHardwareMonitor"
        s["gpu"] = g
        if sens.get("cpu_temp") is not None:
            s["cpu_temp"], s["cpu_src"] = sens["cpu_temp"], "LibreHardwareMonitor"
        elif sens.get("tz") is not None:
            s["cpu_temp"], s["cpu_src"] = sens["tz"], "ACPI thermal zone"
        else:
            s["cpu_temp"], s["cpu_src"] = None, None
        s["cpu_load"] = cpu.read()
        s["ram"] = probes.memory()
        s["net"] = net.read()
        s["ping"], s["ping_error"] = None, ""
        if pinger:
            try:
                s["ping"] = pinger.ping(shared["host"])
                if s["ping"] is None:
                    s["ping_error"] = "timeout"
            except ValueError as e:
                s["ping_error"] = str(e)
                pinger.forget()
        post(s)
        every = shared["interval"]
        if on_battery and not shared["page"] and not s["game"]:
            every = max(every, 5.0)
        time.sleep(max(0.2, every - (time.monotonic() - t0)))
    helper.stop()


@app.on_message("sample")
def on_sample(s):
    g = s.get("gpu") or {}
    info["gpu_src"], info["cpu_src"], info["lhm"] = s.get("gpu_src"), s.get("cpu_src"), s.get("lhm")
    if s.get("gpu_src") == "nvidia-smi":
        info["gpu_name"] = g.get("name") or info["gpu_name"]
    elif g:
        info["gpu_name"] = info["gpu_name"] or "GPU"
    if g:
        info["fan_unit"], info["vram_total"] = g.get("fan_unit"), g.get("vram_total")
    info["ping_error"] = s.get("ping_error", "")
    info["power"], info["paused_gpu"] = s.get("power"), s.get("paused_gpu")
    ram = s.get("ram") or (None, None, None)
    net = s.get("net") or (None, None)
    info["ram_total"] = ram[1] or info["ram_total"]
    vals = {"gpu_temp": g.get("temp"), "gpu_load": g.get("load"), "gpu_power": g.get("power"),
            "gpu_clock": g.get("clock"), "gpu_fan": g.get("fan"), "vram": g.get("vram_used"),
            "cpu_temp": s.get("cpu_temp"), "cpu_load": s.get("cpu_load"), "ram": ram[0],
            "net_down": net[0], "net_up": net[1], "ping": s.get("ping")}
    if s.get("paused_gpu"):
        for m in ("gpu_temp", "gpu_load", "gpu_power", "gpu_clock", "gpu_fan", "vram"):
            vals[m] = None
    for m, v in vals.items():
        cur[m] = v
        hist[m].append(v if v is not None else 0)
    if s.get("ping") is not None or s.get("ping_error") == "timeout":
        pings.append((s.get("ping") is not None, s.get("ping") or 0))
    info["started"] = True
    track_game(s.get("game"))
    check_alerts()
    draw()


# Game sessions ------------------------------------------------------------------------

def game_name(g):
    t = (g.get("title") or "").strip()
    if t and len(t) <= 40:
        return t
    name = g["exe"][:-4] if g["exe"].endswith(".exe") else g["exe"]
    for junk in ("-Win64-Shipping", "-Win64", "_x64", "64", "Client"):
        name = name.replace(junk, "")
    return name[:1].upper() + name[1:]


def track_game(g):
    now = time.time()
    live = game["live"]
    if g:
        seen = ui["seen"]
        if not seen or seen["exe"] != g["exe"]:
            ui["seen"] = seen = {"exe": g["exe"], "first": now}
        seen["last"] = now
        if not live and now - seen["first"] >= GAME_AFTER:
            game["live"] = live = {"exe": g["exe"], "name": game_name(g), "start": seen["first"],
                                   "gpu_peak": None, "cpu_peak": None, "load_sum": 0.0, "load_n": 0,
                                   "ping_sum": 0.0, "ping_n": 0, "ping_max": None, "vram_peak": None}
            app.active(True)
        if live and live["exe"] == g["exe"]:
            live["last"] = now
            for m, k in (("gpu_temp", "gpu_peak"), ("cpu_temp", "cpu_peak"), ("vram", "vram_peak"),
                         ("ping", "ping_max")):
                if cur[m] is not None:
                    live[k] = cur[m] if live[k] is None else max(live[k], cur[m])
            if cur["gpu_load"] is not None:
                live["load_sum"] += cur["gpu_load"]
                live["load_n"] += 1
            if cur["ping"] is not None:
                live["ping_sum"] += cur["ping"]
                live["ping_n"] += 1
    if live and now - live.get("last", now) > GAME_GRACE:
        end_game()


def end_game():
    live = game["live"]
    game["live"] = None
    app.active(False)
    if not live or live.get("last", live["start"]) - live["start"] < 60:
        return
    sessions.append({"name": live["name"], "exe": live["exe"], "start": live["start"],
                     "minutes": round((live["last"] - live["start"]) / 60, 1),
                     "gpu_peak": live["gpu_peak"], "cpu_peak": live["cpu_peak"],
                     "gpu_load": round(live["load_sum"] / live["load_n"]) if live["load_n"] else None,
                     "ping": round(live["ping_sum"] / live["ping_n"]) if live["ping_n"] else None,
                     "ping_max": live["ping_max"], "vram_peak": live["vram_peak"]})
    del sessions[:-50]
    app.save("sessions", sessions)


# Alerts -------------------------------------------------------------------------------

def check_alerts():
    hot = None
    gt, ct = cur["gpu_temp"], cur["cpu_temp"]
    was = ui["alert"]
    if gt is not None and (gt >= settings["gpu_alert"] or (was == "gpu" and gt >= settings["gpu_alert"] - 5)):
        hot = "gpu"
    elif info["cpu_src"] == "LibreHardwareMonitor" and ct is not None and (
            ct >= settings["cpu_alert"] or (was == "cpu" and ct >= settings["cpu_alert"] - 5)):
        hot = "cpu"   # the ACPI zone isn't the CPU, so it never alerts
    if hot != was:
        ui["alert"] = hot
        if hot:
            app.badge_glyph("thermometer-hot")
        else:
            app.clear_badge()


# Formatting -------------------------------------------------------------------------

def rate(bps):
    if bps is None:
        return "–"
    for unit, div in (("GB/s", 1e9), ("MB/s", 1e6), ("KB/s", 1e3)):
        if bps >= div:
            return f"{bps / div:.1f} {unit}" if bps / div < 100 else f"{bps / div:.0f} {unit}"
    return f"{bps:.0f} B/s"


def fmt(m, v=None):
    v = cur[m] if v is None else v
    if v is None:
        if m == "ping" and info["ping_error"] == "timeout":
            return "timeout"
        if m.startswith("gpu") or m == "vram":
            return "asleep" if info["paused_gpu"] else "–"
        return "–"
    if m.endswith("temp"):
        return f"{v:.0f}°"
    if m == "gpu_fan":
        return f"{v:.0f} rpm" if info["fan_unit"] == "rpm" else f"{v:.0f}%"
    if m == "vram":
        total = info["vram_total"]
        return f"{v / 1024:.1f}/{total / 1024:.0f} GB" if total else f"{v / 1024:.1f} GB"
    if m == "ram":
        return f"{v:.1f}/{info['ram_total']:.0f} GB" if info["ram_total"] else f"{v:.1f} GB"
    if m == "ping":
        return f"{v:.0f} ms"
    if m == "gpu_power":
        return f"{v:.0f} W" if v >= 10 else f"{v:.1f} W"
    if m == "gpu_clock":
        return f"{v / 1000:.2f} GHz" if v >= 1000 else f"{v:.0f} MHz"
    if m.startswith("net"):
        return rate(v)
    return f"{v:.0f}%"


def temp_color(v):
    if v is None:
        return "dim"
    return "red" if v >= 85 else "orange" if v >= 75 else "yellow" if v >= 65 else "green"


def ping_color(v):
    if v is None:
        return "red" if info["ping_error"] else "dim"
    return "red" if v >= 150 else "orange" if v >= 80 else "yellow" if v >= 40 else "green"


def color(m):
    v = cur[m]
    if m.endswith("temp"):
        return temp_color(v) if v is not None else "dim"
    if m == "ping":
        return ping_color(v)
    if v is None:
        return "dim"
    if m.endswith("load"):
        return "orange" if v >= 95 else "text"
    return "text"


def ping_stats():
    oks = [ms for ok, ms in pings if ok]
    loss = 100 * (len(pings) - len(oks)) / len(pings) if pings else 0
    jitter = sum(abs(a - b) for a, b in zip(oks, oks[1:])) / (len(oks) - 1) if len(oks) > 1 else 0
    avg = sum(oks) / len(oks) if oks else None
    return avg, jitter, loss


SPARK_COLOR = {"gpu_temp": "orange", "gpu_load": "green", "gpu_power": "orange", "gpu_clock": "yellow",
               "gpu_fan": "teal", "vram": "purple", "cpu_temp": "red", "cpu_load": "blue", "ram": "blue",
               "net_down": "teal", "net_up": "pink", "ping": "accent"}
LABELS = {"gpu_temp": "GPU temp", "gpu_load": "GPU load", "gpu_power": "GPU power", "gpu_clock": "GPU clock",
          "gpu_fan": "GPU fan", "vram": "VRAM", "cpu_temp": "CPU temp", "cpu_load": "CPU load", "ram": "RAM",
          "net_down": "Download", "net_up": "Upload", "ping": "Ping"}


def spark_max(m):
    if m == "vram":
        return info["vram_total"]
    if m == "ram":
        return info["ram_total"]
    if m in ("gpu_temp", "gpu_load", "cpu_temp", "cpu_load") or (m == "gpu_fan" and info["fan_unit"] == "%"):
        return 100
    return None


# Gauges --------------------------------------------------------------------------------

def gauge(cx, cy, r, value, top, label, sub, col, width=7, big=22):
    """An arc gauge from -120° to 120°, value 0..1."""
    shapes = [fenn.arc(cx, cy, r, -120, 120, color="faint", width=width)]
    if value is not None and value > 0.005:
        shapes.append(fenn.arc(cx, cy, r, -120, -120 + 240 * min(1.0, value), color=col, width=width))
    shapes.append(fenn.label(cx, cy - 2, top, size=big, color="text" if value is not None else "dim",
                             anchor="center", bold=True))
    if sub:
        shapes.append(fenn.label(cx, cy + big * 0.72, sub, size=10, color="dim", anchor="center"))
    shapes.append(fenn.label(cx, cy + r * 0.86 + 2, label, size=11, color="dim", anchor="center"))
    return shapes


def gauges_canvas():
    w = max(320, ui["page_w"] - 32)
    h = 118
    slot = w / 4
    r = min(40, slot / 2 - 10)
    cy = 52
    gt, gl, ct, cl = cur["gpu_temp"], cur["gpu_load"], cur["cpu_temp"], cur["cpu_load"]
    clock_sub = fmt("gpu_clock") if cur["gpu_clock"] is not None else ""
    power_sub = fmt("gpu_power") if cur["gpu_power"] is not None else ""
    shapes = []
    shapes += gauge(slot * 0.5, cy, r, None if gt is None else gt / 100, fmt("gpu_temp"), "GPU temp",
                    power_sub, temp_color(gt))
    shapes += gauge(slot * 1.5, cy, r, None if gl is None else gl / 100, fmt("gpu_load"), "GPU load",
                    clock_sub, "accent")
    shapes += gauge(slot * 2.5, cy, r, None if ct is None else ct / 100, fmt("cpu_temp"), "CPU temp",
                    "board sensor" if info["cpu_src"] == "ACPI thermal zone" else "", temp_color(ct))
    shapes += gauge(slot * 3.5, cy, r, None if cl is None else cl / 100, fmt("cpu_load"), "CPU load",
                    "", "blue")
    return fenn.canvas(h, shapes, width=w)


def tile_gauges(w, h):
    """Four small gauges for a large tile."""
    cw, ch = w / 2, h / 2
    r = min(cw, ch) / 2 - 5
    gt, gl, ct, pg = cur["gpu_temp"], cur["gpu_load"], cur["cpu_temp"], cur["ping"]
    shapes = []
    cells = [(gt, gt / 100 if gt is not None else None, fmt("gpu_temp"), "GPU", temp_color(gt)),
             (gl, gl / 100 if gl is not None else None, f"{gl:.0f}" if gl is not None else "–", "load", "accent"),
             (ct, ct / 100 if ct is not None else None, fmt("cpu_temp"), "CPU", temp_color(ct)),
             (pg, min(1.0, pg / 150) if pg is not None else None, f"{pg:.0f}" if pg is not None else "–", "ms",
              ping_color(pg))]
    for i, (_, val, top, lab, col) in enumerate(cells):
        cx, cy = cw * (i % 2) + cw / 2, ch * (i // 2) + ch / 2 + 1
        shapes.append(fenn.arc(cx, cy, r, -120, 120, color="faint", width=4))
        if val:
            shapes.append(fenn.arc(cx, cy, r, -120, -120 + 240 * min(1.0, val), color=col, width=4))
        shapes.append(fenn.label(cx, cy - 1, top, size=12, anchor="center", bold=True,
                                 color="text" if val is not None else "dim"))
        shapes.append(fenn.label(cx, cy + r * 0.8, lab, size=8, anchor="center", color="dim"))
    return fenn.canvas(h, shapes, width=w)


# Views ---------------------------------------------------------------------------------

def tile_view():
    size = app.tile_size or "wide"
    w, h = app.tile_width or 104, app.tile_height or 36
    avg, jitter, loss = ping_stats()
    tip = "\n".join(f"{LABELS[m]}: {fmt(m)}" for m in METRICS if cur[m] is not None or m == "ping")
    if game["live"]:
        tip = f"Playing {game['live']['name']} for {int((time.time() - game['live']['start']) // 60)} min\n" + tip
    if pings:
        tip += f"\nJitter {jitter:.1f} ms · loss {loss:.0f}%"
    if not info["started"]:
        return fenn.column([fenn.icon("game-controller", size=20, color="dim")], align="center",
                           justify="center", tooltip="Starting sensors…")
    gpu_ok = info["gpu_src"] is not None or info["paused_gpu"]
    if size == "small":
        m = "gpu_temp" if gpu_ok and cur["gpu_temp"] is not None else "cpu_temp"
        return fenn.column([fenn.text(fmt(m), size="title", color=color(m)),
                            fenn.text("GPU" if m == "gpu_temp" else "CPU", size="small", color="dim")],
                           gap=0, align="center", justify="center", tooltip=tip)
    if size == "large":
        return fenn.column([tile_gauges(w, h)], gap=0, justify="center", tooltip=tip)

    def cell(label, m, m2=None):
        parts = ([fenn.text(label, size="small", color="faint")] if label else []) + [
            fenn.text(fmt(m), size="small", bold=True, color=color(m))]
        if m2 and cur[m2] is not None:
            parts.append(fenn.text(fmt(m2), size="small", color=color(m2) if color(m2) != "text" else "dim"))
        return fenn.row(parts, gap=3, align="center")

    lead = [fenn.icon("game-controller", size=12, color="green", fill=True)] if game["live"] else []
    first = fenn.row(lead + [cell("GPU", "gpu_temp", "gpu_load")], gap=3, align="center")
    tail = [cell("", "ping")]
    pw = info["power"]
    if pw and pw["battery"] and not pw["ac"]:
        tail.insert(0, fenn.icon("battery-warning" if game["live"] else "battery-medium", size=12,
                                 color="orange" if game["live"] else "dim"))
    second = fenn.row([cell("CPU", "cpu_temp"), fenn.spacer(), *tail], gap=3, align="center")
    return fenn.column([first, second], gap=1, justify="center", tooltip=tip)


def metric_card(m, value=None, sub=None, spark=None, smax=None, col=None):
    vals = list(hist[spark or m])
    has = any(vals)
    kids = [fenn.row([fenn.text(LABELS[m], size="small", color="dim"), fenn.spacer(),
                      fenn.text(value or fmt(m), bold=True, color=color(m))], gap=4, align="center")]
    if sub:
        kids.append(fenn.text(sub, size="small", color="faint"))
    kids.append(fenn.sparkline(vals, max=smax if smax is not None else spark_max(spark or m),
                               color=col or SPARK_COLOR[m], height=30) if has
                else fenn.text("no data", size="small", color="faint"))
    return fenn.card(kids, gap=3, padding=8)


def power_chip():
    pw = info["power"]
    if not pw or not pw["battery"]:
        return []
    pct = f"{pw['percent']}%" if pw["percent"] is not None else ""
    if pw["ac"]:
        return [fenn.icon("plug-charging", size=14, color="green"), fenn.text(pct, size="small", color="dim")]
    return [fenn.icon("battery-warning" if game["live"] else "battery-medium", size=14,
                      color="orange" if game["live"] or (pw["percent"] or 100) < 25 else "dim"),
            fenn.text(f"{pct} on battery" + (" · saver" if pw["saver"] else ""), size="small",
                      color="orange" if game["live"] else "dim")]


def game_card():
    g = game["live"]
    if g:
        mins = int((time.time() - g["start"]) // 60)
        avg_load = f"{g['load_sum'] / g['load_n']:.0f}%" if g["load_n"] else "–"
        avg_ping = f"{g['ping_sum'] / g['ping_n']:.0f} ms" if g["ping_n"] else "–"
        peaks = [f"GPU peak {g['gpu_peak']:.0f}°" if g["gpu_peak"] is not None else "",
                 f"avg load {avg_load}",
                 f"CPU peak {g['cpu_peak']:.0f}°" if g["cpu_peak"] is not None else "",
                 f"ping {avg_ping}" + (f" (max {g['ping_max']:.0f})" if g["ping_max"] is not None else "")]
        kids = [fenn.row([fenn.icon("game-controller", size=22, color="green", fill=True),
                          fenn.column([fenn.text(g["name"], size="title"),
                                       fenn.text(f"Playing for {mins // 60} h {mins % 60} min" if mins >= 60
                                                 else f"Playing for {mins} min", size="small", color="dim")],
                                      gap=0),
                          fenn.spacer(), *power_chip()], gap=10, align="center"),
                fenn.text(" · ".join(p for p in peaks if p), size="small", color="dim")]
        pw = info["power"]
        if pw and pw["battery"] and not pw["ac"]:
            kids.append(fenn.row([fenn.icon("warning", size=14, color="orange"),
                                  fenn.text("On battery: the GPU runs slower. Plug in for full performance.",
                                            size="small", color="orange")], gap=6, align="center"))
        return fenn.card(kids, gap=6, padding=10)
    if not sessions:
        return None
    items = []
    for s in reversed(sessions[-5:]):
        when = dt.datetime.fromtimestamp(s["start"])
        day = "Today" if when.date() == dt.date.today() else when.strftime("%a %d %b").replace(" 0", " ")
        mins = int(s["minutes"])
        trail = " · ".join(x for x in (f"{s['gpu_peak']:.0f}°" if s.get("gpu_peak") is not None else "",
                                       f"{s['ping']} ms" if s.get("ping") is not None else "") if x)
        items.append(fenn.item(f"sess:{int(s['start'])}", s["name"],
                               subtitle=f"{day} {when.strftime('%H:%M')} · "
                                        + (f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min")
                                        + (f" · avg load {s['gpu_load']}%" if s.get("gpu_load") is not None else ""),
                               icon="game-controller", trailing=trail))
    return fenn.column([fenn.text("Recent game sessions", size="small", color="dim", bold=True),
                        fenn.list_view(items)], gap=4)


def notices():
    out = []
    if info["paused_gpu"]:
        out.append(fenn.row([fenn.icon("battery-medium", size=14, color="dim"), fenn.text(
            "On battery: the GPU isn't read while no game is running, so it can sleep. "
            "Opening this page reads it.", size="small", color="dim", wrap=True)], gap=6))
    elif info["started"] and not info["gpu_src"]:
        out.append(fenn.card([fenn.row([fenn.icon("warning", color="orange"), fenn.text(
            "No GPU sensor source. Install the Nvidia driver (it includes nvidia-smi), or run "
            "LibreHardwareMonitor, which publishes AMD, Intel and Nvidia sensors to WMI while it's open.",
            size="small", wrap=True)], gap=6)], padding=8))
    if info["started"] and info["cpu_src"] != "LibreHardwareMonitor":
        msg = ("CPU temperature is the ACPI thermal zone, which is often the board rather than the cores. "
               "Run LibreHardwareMonitor for the real package temperature."
               if info["cpu_src"] else "No CPU temperature available. Run LibreHardwareMonitor to get one.")
        out.append(fenn.text(msg, size="small", color="faint", wrap=True))
    return out


def page_view():
    header = fenn.row([
        fenn.icon("graphics-card", size=18, color="accent"),
        fenn.text(info["gpu_name"] or "GPU", bold=True),
        fenn.text(f"via {info['gpu_src']}" if info["gpu_src"] else "", size="small", color="faint"),
        fenn.spacer(),
        *power_chip(),
    ], gap=8, align="center")
    avg, jitter, loss = ping_stats()
    ping_sub = f"jitter {jitter:.1f} ms · loss {loss:.0f}% · {settings['host']}" if pings else settings["host"]
    cards = [
        metric_card("ping", sub=ping_sub),
        metric_card("net_down", value=f"↓ {fmt('net_down')}", sub=f"↑ {fmt('net_up')}"),
        metric_card("vram") if info["vram_total"] or any(hist["vram"]) else metric_card("ram"),
        metric_card("ram") if info["vram_total"] or any(hist["vram"]) else metric_card("gpu_power"),
        metric_card("gpu_power", sub=f"clock {fmt('gpu_clock')}" if cur["gpu_clock"] is not None else None),
        metric_card("gpu_fan") if any(hist["gpu_fan"]) else metric_card("cpu_load"),
    ]
    body = [header, gauges_canvas()]
    gc = game_card()
    if gc:
        body.append(gc)
    body.append(fenn.grid(3, cards, gap=6))
    body += notices()
    if info["ping_error"] and info["ping_error"] != "timeout":
        body.append(fenn.text(info["ping_error"], size="small", color="red"))
    body.append(fenn.card([
        fenn.row([fenn.text("Ping host", size="small", color="dim"),
                  fenn.text_input(f"host:{ui['host_n']}", settings["host"], hint=DEFAULT_HOST, width=170,
                                  tooltip="A host or IP to ping, e.g. a game server. Press Enter."),
                  fenn.spacer(),
                  fenn.toggle("saver", settings["saver"], label="Battery saver")], gap=8, align="center"),
        fenn.row([fenn.text("Alert when the GPU reaches", size="small", color="dim"), fenn.spacer(),
                  fenn.text(f"{settings['gpu_alert']}°", size="small", bold=True),
                  fenn.slider("gpu_alert", settings["gpu_alert"], min=70, max=100, step=1, width=180)],
                 gap=8, align="center"),
    ], gap=6, padding=10))
    return fenn.column(body, gap=8, scroll=True)


last_sent = {"tile": None, "page": None}


def draw():
    tile = json.dumps(tile_view(), sort_keys=True)
    if tile != last_sent["tile"]:      # a quiet machine sends nothing
        last_sent["tile"] = tile
        fenn.emit({"type": "tile", "view": json.loads(tile)})
    if app.page_open:
        page = json.dumps(page_view(), sort_keys=True)
        if page != last_sent["page"]:
            last_sent["page"] = page
            app.page(json.loads(page))


# Events -------------------------------------------------------------------------

@app.on_message("event")
def on_event(e):
    eid, value, kind = e.get("id", ""), e.get("value"), e.get("kind")
    if eid.startswith("host:") and kind == "submit":
        host = (value or "").strip() or DEFAULT_HOST
        settings["host"] = shared["host"] = host
        app.save("settings", settings)
        hist["ping"].clear()
        pings.clear()
        ui["host_n"] += 1
    elif eid == "saver":
        settings["saver"] = shared["saver"] = bool(value)
        app.save("settings", settings)
    elif eid == "gpu_alert" and kind in ("change", "commit"):
        settings["gpu_alert"] = int(value or 87)
        if kind == "commit":
            app.save("settings", settings)
            check_alerts()
    else:
        return
    draw()


@app.on_message("page_open")
def on_page_open(_):
    shared["interval"], shared["page"] = 1.0, True
    last_sent["page"] = None
    draw()


@app.on_message("page_close")
def on_page_close(_):
    shared["interval"], shared["page"] = 2.0, False


@app.on_message("layout")
def on_layout(msg):
    if msg.get("target") == "page" and msg.get("width"):
        ui["page_w"] = msg["width"]
    last_sent["tile"] = last_sent["page"] = None
    draw()


@app.on_message("shutdown")
def on_shutdown(_):
    shared["stop"] = True
    if game["live"]:
        game["live"]["last"] = time.time()
        end_game()


app.page(page_view())   # so a click on the tile opens the page
draw()
if probes:
    threading.Thread(target=sampler, daemon=True).start()
app.run()
