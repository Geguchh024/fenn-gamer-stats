# Long-running sensor helper for gamer-stats. For every line read on stdin it
# prints one JSON line:
#   {"lhm": "<namespace>"|null, "gpu": {...}|null, "cpu_temp": n|null, "tz": n|null}
# - LibreHardwareMonitor (or OpenHardwareMonitor) publishes its sensors in WMI
#   while it is running: root/LibreHardwareMonitor, class Sensor.
# - Without it, the ACPI thermal zone counter is the only CPU-ish temperature
#   Windows offers without admin rights (often the board, not the cores).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$namespaces = @('root/LibreHardwareMonitor', 'root/OpenHardwareMonitor')
$ns = $null
$nsCheckedAt = [DateTime]::MinValue

function Find-Namespace {
    foreach ($n in $namespaces) {
        try {
            $null = Get-CimInstance -Namespace $n -ClassName Sensor -Filter "SensorType='Temperature'" | Select-Object -First 1
            return $n
        } catch { }
    }
    return $null
}

function Pick($sensors, $type, [string[]]$names) {
    foreach ($name in $names) {
        $s = $sensors | Where-Object { $_.SensorType -eq $type -and $_.Name -eq $name } | Select-Object -First 1
        if ($s) { return [double]$s.Value }
    }
    $null
}

while ($null -ne ($line = [Console]::In.ReadLine())) {
    $out = @{ lhm = $null; gpu = $null; cpu_temp = $null; tz = $null }
    if (-not $ns -and ([DateTime]::Now - $nsCheckedAt).TotalSeconds -gt 20) {
        $ns = Find-Namespace
        $nsCheckedAt = [DateTime]::Now
    }
    if ($ns) {
        try {
            $all = @(Get-CimInstance -Namespace $ns -ClassName Sensor)
            $out.lhm = $ns
            $gpu = @($all | Where-Object { $_.Identifier -like '/gpu*' })
            if ($gpu.Count) {
                $fanRpm = Pick $gpu 'Fan' @('GPU Fan', 'GPU Fan 1', 'Fan 1')
                $fanPct = Pick $gpu 'Control' @('GPU Fan', 'GPU Fan 1', 'Fan 1')
                $out.gpu = @{
                    temp       = Pick $gpu 'Temperature' @('GPU Core', 'GPU Hot Spot')
                    load       = Pick $gpu 'Load' @('GPU Core', 'D3D 3D')
                    fan        = if ($null -ne $fanPct) { $fanPct } else { $fanRpm }
                    fan_unit   = if ($null -ne $fanPct) { '%' } elseif ($null -ne $fanRpm) { 'rpm' } else { $null }
                    vram_used  = Pick $gpu 'SmallData' @('GPU Memory Used', 'D3D Dedicated Memory Used')
                    vram_total = Pick $gpu 'SmallData' @('GPU Memory Total')
                }
            }
            $cpu = @($all | Where-Object { $_.Identifier -like '/*cpu/*' })
            $out.cpu_temp = Pick $cpu 'Temperature' @('CPU Package', 'Core (Tctl/Tdie)', 'Core Max', 'Core Average', 'CPU Cores')
        } catch {
            $ns = $null   # LibreHardwareMonitor was closed; look again later
        }
    }
    if ($null -eq $out.cpu_temp) {
        try {
            $z = Get-CimInstance -ClassName Win32_PerfFormattedData_Counters_ThermalZoneInformation |
                Sort-Object HighPrecisionTemperature -Descending | Select-Object -First 1
            if ($z -and $z.HighPrecisionTemperature -gt 0) { $out.tz = [math]::Round($z.HighPrecisionTemperature / 10 - 273.15, 1) }
        } catch { }
    }
    [Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 5 -Compress))
    [Console]::Out.Flush()
}
