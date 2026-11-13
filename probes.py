"""Sensor readers for gamer-stats, stdlib only:

- Nvidia: nvidia-smi's CSV query (ships with the driver).
- LibreHardwareMonitor / thermal zone: a long-running PowerShell helper
  (sensors.ps1) that answers one JSON line per request, so WMI isn't paid
  for with a new process every sample.
- CPU load: GetSystemTimes. RAM: GlobalMemoryStatusEx. Power: GetSystemPowerStatus.
- Network: GetIfTable byte counters, one row per physical adapter.
- Ping: IcmpSendEcho from iphlpapi, which needs no admin rights.
- Games: the foreground window covering its whole monitor."""

import base64
import ctypes
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from ctypes import byref, c_ulong, c_ushort, c_void_p, wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = 0x08000000


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None   # "[N/A]", "[Not Supported]"


class Nvidia:
    FIELDS = ("name,temperature.gpu,utilization.gpu,fan.speed,memory.used,memory.total,"
              "power.draw,clocks.gr,clocks.mem")

    def __init__(self):
        self.exe = shutil.which("nvidia-smi") or next(
            (p for p in (os.path.expandvars(r"%SystemRoot%\System32\nvidia-smi.exe"),
                         os.path.expandvars(r"%ProgramFiles%\NVIDIA Corporation\NVSMI\nvidia-smi.exe"))
             if os.path.isfile(p)), None)

    def read(self):
        """First GPU as a dict, or None."""
        if not self.exe:
            return None
        try:
            r = subprocess.run([self.exe, f"--query-gpu={self.FIELDS}", "--format=csv,noheader,nounits"],
                               capture_output=True, timeout=5, creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return None
        lines = r.stdout.decode("utf-8", "ignore").strip().splitlines()
        if r.returncode != 0 or not lines:
            return None
        parts = [p.strip() for p in lines[0].split(",")]
        if len(parts) < 6:
            return None
        parts += [""] * (9 - len(parts))
        fan = _num(parts[3])
        return {"name": parts[0], "temp": _num(parts[1]), "load": _num(parts[2]), "fan": fan,
                "fan_unit": "%" if fan is not None else None, "vram_used": _num(parts[4]),
                "vram_total": _num(parts[5]), "power": _num(parts[6]), "clock": _num(parts[7]),
                "mem_clock": _num(parts[8])}


class SensorHelper:
    """Keeps sensors.ps1 running; read() asks it for one sample."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.failures = 0

    def _start(self):
        with open(os.path.join(HERE, "sensors.ps1"), encoding="utf-8") as f:
            script = base64.b64encode(f.read().encode("utf-16-le")).decode("ascii")
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW)

    def read(self):
        with self.lock:
            if self.failures > 5:
                return None
            try:
                if not self.proc or self.proc.poll() is not None:
                    self._start()
                self.proc.stdin.write(b"sample\n")
                self.proc.stdin.flush()
                line = self.proc.stdout.readline()
                data = json.loads(line.decode("utf-8", "ignore"))
                self.failures = 0
                return data
            except (OSError, ValueError):
                self.failures += 1
                self.stop()
                return None

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
            self.proc = None


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def value(self):
        return (self.hi << 32) | self.lo


class CpuLoad:
    def __init__(self):
        self.last = None

    def read(self):
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(byref(idle), byref(kernel), byref(user)):
            return None
        now = (idle.value(), kernel.value() + user.value())  # kernel time includes idle
        last, self.last = self.last, now
        if not last or now[1] == last[1]:
            return None
        busy = (now[1] - last[1]) - (now[0] - last[0])
        return max(0.0, min(100.0, 100.0 * busy / (now[1] - last[1])))


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [("Address", c_ulong), ("Status", c_ulong), ("RoundTripTime", c_ulong),
                ("DataSize", c_ushort), ("Reserved", c_ushort), ("Data", c_void_p),
                ("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", c_void_p)]


class Pinger:
    def __init__(self):
        self.api = ctypes.WinDLL("iphlpapi")
        self.api.IcmpCreateFile.restype = c_void_p
        self.api.IcmpCloseHandle.argtypes = [c_void_p]
        self.api.IcmpSendEcho.argtypes = [c_void_p, c_ulong, c_void_p, c_ushort, c_void_p, c_void_p,
                                          wintypes.DWORD, wintypes.DWORD]
        self.handle = self.api.IcmpCreateFile()
        self.cache = {}

    def ping(self, host, timeout_ms=1000):
        """Round trip in ms, or None on timeout / unknown host. Raises
        ValueError if the host name can't be resolved."""
        if host not in self.cache:
            try:
                self.cache[host] = socket.gethostbyname(host)
            except (OSError, UnicodeError):
                raise ValueError(f"Can't resolve {host}")
        addr = int.from_bytes(socket.inet_aton(self.cache[host]), "little")
        payload = b"fenn-gamer-stats"
        size = ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8
        reply = ctypes.create_string_buffer(size)
        n = self.api.IcmpSendEcho(self.handle, addr, payload, len(payload), None, reply, size, timeout_ms)
        if not n:
            return None
        r = ICMP_ECHO_REPLY.from_buffer_copy(reply)
        return float(r.RoundTripTime) if r.Status == 0 else None

    def forget(self):
        self.cache.clear()


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def memory():
    """(used GB, total GB, load %)"""
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(byref(m)):
        return None
    gb = 1024 ** 3
    return (m.ullTotalPhys - m.ullAvailPhys) / gb, m.ullTotalPhys / gb, float(m.dwMemoryLoad)


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def power():
    """{"battery": has one, "ac": plugged in, "percent", "saver": Windows battery saver on}"""
    s = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(byref(s)):
        return None
    has = not (s.BatteryFlag & 128) and s.BatteryFlag != 255
    return {"battery": has, "ac": s.ACLineStatus == 1 or not has,
            "percent": s.BatteryLifePercent if has and s.BatteryLifePercent <= 100 else None,
            "saver": bool(s.SystemStatusFlag & 1)}


class MIB_IFROW(ctypes.Structure):
    _fields_ = [("wszName", ctypes.c_wchar * 256), ("dwIndex", wintypes.DWORD), ("dwType", wintypes.DWORD),
                ("dwMtu", wintypes.DWORD), ("dwSpeed", wintypes.DWORD), ("dwPhysAddrLen", wintypes.DWORD),
                ("bPhysAddr", ctypes.c_ubyte * 8), ("dwAdminStatus", wintypes.DWORD),
                ("dwOperStatus", wintypes.DWORD), ("dwLastChange", wintypes.DWORD),
                ("dwInOctets", wintypes.DWORD), ("dwInUcastPkts", wintypes.DWORD),
                ("dwInNUcastPkts", wintypes.DWORD), ("dwInDiscards", wintypes.DWORD),
                ("dwInErrors", wintypes.DWORD), ("dwInUnknownProtos", wintypes.DWORD),
                ("dwOutOctets", wintypes.DWORD), ("dwOutUcastPkts", wintypes.DWORD),
                ("dwOutNUcastPkts", wintypes.DWORD), ("dwOutDiscards", wintypes.DWORD),
                ("dwOutErrors", wintypes.DWORD), ("dwOutQLen", wintypes.DWORD),
                ("dwDescrLen", wintypes.DWORD), ("bDescr", ctypes.c_ubyte * 256)]


class Network:
    """Download and upload rate in bytes per second across connected
    Ethernet and Wi-Fi adapters. GetIfTable lists each adapter several times
    (one row per filter driver), so rows are merged by MAC address."""

    def __init__(self):
        self.api = ctypes.WinDLL("iphlpapi")
        self.last = None

    def _totals(self):
        size = c_ulong(0)
        self.api.GetIfTable(None, byref(size), False)
        buf = ctypes.create_string_buffer(size.value + 64)
        if self.api.GetIfTable(buf, byref(size), False) != 0:
            return None
        n = wintypes.DWORD.from_buffer(buf).value
        row = ctypes.sizeof(MIB_IFROW)
        per_mac = {}
        for i in range(n):
            r = MIB_IFROW.from_buffer(buf, 4 + i * row)
            if r.dwType not in (6, 71) or r.dwOperStatus < 4 or not r.dwPhysAddrLen:
                continue
            mac = bytes(r.bPhysAddr[:r.dwPhysAddrLen])
            a, b = per_mac.get(mac, (0, 0))
            per_mac[mac] = (max(a, r.dwInOctets), max(b, r.dwOutOctets))
        return per_mac

    def read(self):
        """(down B/s, up B/s), or None on the first call."""
        now, cur = time.monotonic(), self._totals()
        if cur is None:
            return None
        last, self.last = self.last, (now, cur)
        if not last or now - last[0] <= 0:
            return None
        span = now - last[0]
        down = up = 0
        for mac, (i, o) in cur.items():
            pi, po = last[1].get(mac, (i, o))
            down += (i - pi) % (1 << 32)
            up += (o - po) % (1 << 32)
        return down / span, up / span


# Games: the foreground window fills its monitor (fullscreen or borderless).
NOT_GAMES = {"explorer.exe", "fenn.exe", "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
             "opera.exe", "vivaldi.exe", "zen.exe", "arc.exe", "vlc.exe", "mpc-hc64.exe", "mpv.exe",
             "powerpnt.exe", "applicationframehost.exe", "lockapp.exe", "searchhost.exe",
             "shellexperiencehost.exe", "startmenuexperiencehost.exe", "textinputhost.exe",
             "python.exe", "pythonw.exe", "wmplayer.exe", "acrobat.exe", "devenv.exe", "code.exe",
             "claude.exe", "windowsterminal.exe", "obs64.exe"}


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


_user32 = ctypes.WinDLL("user32")
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetShellWindow.restype = wintypes.HWND
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_kernel32 = ctypes.WinDLL("kernel32")
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _exe(pid):
    h = _kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _kernel32.CloseHandle(h)


def foreground_game():
    """{"exe", "title"} when a fullscreen app has the focus, else None."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd or hwnd == _user32.GetShellWindow():
        return None
    r = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, byref(r)):
        return None
    mon = _user32.MonitorFromWindow(hwnd, 2)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if not mon or not _user32.GetMonitorInfoW(mon, byref(mi)):
        return None
    m = mi.rcMonitor
    if not (r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom):
        return None
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, byref(pid))
    exe = _exe(pid.value)
    if not exe or exe in NOT_GAMES:
        return None
    n = _user32.GetWindowTextLengthW(hwnd)
    title = ""
    if n > 0:
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
    return {"exe": exe, "title": title}
