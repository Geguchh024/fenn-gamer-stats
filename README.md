# Gamer Stats

> GPU and CPU temperatures, load, VRAM, network speed and ping with jitter
> and packet loss in a compact tile next to your game. It also times your
> game sessions automatically.

![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Python](https://img.shields.io/badge/python-3.8%2B-3776AB)
![Version](https://img.shields.io/badge/version-2.0.0-7C5CFF)
![Plugin for](https://img.shields.io/badge/plugin%20for-Fenn-111)

A plugin for **Fenn**, the floating notch and taskbar for Windows. No admin
rights and nothing to install.

| | |
|---|---|
| **Tile** | wide (small and large also supported) |
| **Page** | 640 × 440 |
| **GPU sources** | `nvidia-smi`, or LibreHardwareMonitor / OpenHardwareMonitor (AMD, Intel) |
| **Network access** | pings only |
| **Dependencies** | none |

## Highlights

- **At-a-glance tile** with color-coded GPU and CPU temperature, GPU load
  and ping.
- **Gauges and sparklines** for GPU temperature, load, power, clock, fan and
  VRAM; CPU temperature and load; RAM; download and upload speed.
- **Ping with jitter and packet loss** to any host, such as your game server.
- **Automatic game sessions** with play time, peak temperatures, average
  load and worst ping.
- **Heat alert** when your GPU gets too hot.
- **Laptop-friendly.** It warns when you game on battery and leaves a
  sleeping GPU alone.

## Install

Clone the repo into Fenn's plugins folder. The folder name must be `gamer-stats`:

```powershell
git clone https://github.com/Geguchh024/fenn-gamer-stats "$env:APPDATA\Fenn\plugins\gamer-stats"
```

You can also download the ZIP and extract it to the same place.

> **AMD or Intel GPU?** Install and run
> [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
> for GPU readings and an accurate CPU package temperature. Nvidia GPUs work
> out of the box through `nvidia-smi`, which comes with the driver.

## The tile

```
🎮 GPU 64° 97%
   CPU 71°      23 ms
```

| Reading | Green | Yellow | Orange | Red |
|---|---|---|---|---|
| Temperature | below 65° | 65° | 75° | 85° |
| Ping | below 40 ms | 40 ms | 80 ms | 150 ms |

Small tiles show just the GPU temperature. Large tiles show four small gauges:
GPU temperature and load, CPU temperature, and ping.

## The page

- **Gauges** for GPU temperature (with power draw), GPU load (with clock), CPU
  temperature and CPU load.
- **Cards with sparklines** of the last 90 samples: ping with **jitter** and
  **packet loss**, download and upload speed, VRAM, RAM, GPU power and clock,
  and the GPU fan when the sensor has one.
- **Game sessions.** A fullscreen or borderless app that keeps the focus for
  20 seconds counts as a game. Browsers, video players and Windows itself
  don't count. While you play, the tile shows a controller, and the page shows
  how long you've played, the peak GPU and CPU temperature, average GPU load,
  and average and worst ping. Alt-tabbing away for under 45 seconds keeps the
  session going. The last five sessions are listed afterwards.
- **Heat alert.** When the GPU reaches the temperature you set (87° by
  default), the collapsed pill shows a hot thermometer until it cools down
  by 5°.
- **Laptops.** The power chip shows the battery level and whether you're
  plugged in, and warns while you play on battery (the GPU runs slower). With
  **Battery saver** on (the default), the GPU isn't polled at all while you're
  on battery with no game running and the page closed, because polling keeps a
  sleeping laptop GPU awake.
- **Ping host.** `1.1.1.1` by default. Type a game server or another host and
  press Enter.

## Where the numbers come from

| Reading | Source |
|---|---|
| GPU | `nvidia-smi` (installed with the Nvidia driver). Without it, **LibreHardwareMonitor**'s WMI namespace (`root/LibreHardwareMonitor`, or OpenHardwareMonitor's), which covers AMD and Intel GPUs but only while that app is running. With neither, the page says so. |
| CPU temperature | LibreHardwareMonitor (package temperature) while it runs. Otherwise the ACPI thermal zone, which Windows exposes without admin rights but which is often a board sensor; the page says that too. |
| CPU load, RAM, battery | `GetSystemTimes`, `GlobalMemoryStatusEx`, `GetSystemPowerStatus` |
| Network | `GetIfTable` byte counters for connected Ethernet and Wi-Fi adapters |
| Ping | ICMP echo through `IcmpSendEcho` (no admin rights needed) |
| Games | The foreground window covering its whole monitor |

WMI is read by one long-running PowerShell helper (`sensors.ps1`) that
answers one line per sample, instead of starting PowerShell every time.

## Performance

- Samples are taken every second while the page is open, every 2 seconds
  otherwise, and every 5 seconds on battery.
- The tile is only redrawn when a displayed number changes.

## Privacy

Game sessions are kept in `sessions.json` in
`%APPDATA%\Fenn\plugin-data\gamer-stats`. Nothing is sent anywhere except the
pings to the host you choose.

## Files

| File | What it is |
|---|---|
| `main.py` | Tile, page, game session tracking and alerts |
| `probes.py` | CPU, RAM, network, ping, battery and foreground-window readings |
| `sensors.ps1` | Long-running PowerShell helper for WMI sensors |
| `fenn.py` | The Fenn plugin helper |
| `plugin.toml` | Plugin manifest |

## Requirements

- Windows 10 or 11
- Python 3.8+ on `PATH` as `python`
- Windows PowerShell (built in)
- Optional: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) for AMD and Intel GPUs
