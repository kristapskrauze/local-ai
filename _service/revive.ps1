$ErrorActionPreference = "Continue"

$ServiceName = "SysMonitor"
$Port = 8765
$Nssm = Join-Path $PSScriptRoot "nssm.exe"
$AdminScript = Join-Path $PSScriptRoot "revive-admin.cmd"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-ServiceControlRight {
    $sddl = (& sc.exe sdshow $ServiceName 2>$null) -join ""
    return ($sddl -match "\(A;;[^)]*RP[^)]*WP[^)]*;;;IU\)")
}

function Get-PortListeners {
    try {
        return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
    } catch {
        $lines = @(netstat -ano | Select-String (":$Port\s+.*LISTENING"))
        $items = @()
        foreach ($line in $lines) {
            $parts = ($line.Line -split "\s+") | Where-Object { $_ }
            if ($parts.Count -ge 5) {
                $items += [pscustomobject]@{ LocalAddress = $parts[1]; OwningProcess = [int]$parts[4] }
            }
        }
        return $items
    }
}

function Show-State {
    Write-Host "Service state:"
    try {
        $svc = Get-Service -Name $ServiceName -ErrorAction Stop
        Write-Host ("  {0}" -f $svc.Status)
    } catch {
        Write-Host ("  unavailable: {0}" -f $_.Exception.Message)
    }
    Write-Host "Port $Port listener:"
    $listeners = @(Get-PortListeners)
    if (-not $listeners) {
        Write-Host "  none"
        return
    }
    foreach ($listener in $listeners) {
        Write-Host ("  {0} pid={1}" -f $listener.LocalAddress, $listener.OwningProcess)
    }
}

function Get-Health {
    try {
        $health = Invoke-RestMethod -TimeoutSec 5 "http://127.0.0.1:$Port/api/health"
        if ($health.elevated) {
            return $health
        }
    } catch {
        return $null
    }
    return $null
}

function Wait-ForHealth {
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 2
        $health = Get-Health
        if ($null -ne $health) {
            return $health
        }
    }
    return $null
}

function Wait-ForRunningState {
    for ($i = 0; $i -lt 10; $i++) {
        try {
            $svc = Get-Service -Name $ServiceName -ErrorAction Stop
            if ($svc.Status -eq "Running") {
                return
            }
        } catch {
            return
        }
        Start-Sleep -Seconds 1
    }
}

Write-Host "=== SysMonitor safe revive ==="
Write-Host "This script will not trigger UAC or switch to the secure desktop."
Write-Host ""

if (-not (Test-Path -LiteralPath $Nssm)) {
    Write-Host "ERROR: NSSM was not found at `"$Nssm`"."
    exit 2
}

Show-State

if (-not (Test-IsAdmin) -and -not (Test-ServiceControlRight)) {
    Write-Host ""
    Write-Host "This shell can query $ServiceName, but it cannot start/stop it yet."
    Write-Host "I will NOT kill the current port $Port listener, because that would leave the dashboard offline."
    Write-Host ""
    Write-Host "One-time repair:"
    Write-Host "  Open Command Prompt or PowerShell as administrator, then run:"
    Write-Host "  `"$AdminScript`""
    Write-Host ""
    Write-Host "After that, plain _service\revive.cmd can recover the service without UAC."
    exit 5
}

Write-Host ""
Write-Host "Stopping listeners on port $Port..."
$listeners = @(Get-PortListeners)
$serviceStatus = "Unknown"
try {
    $serviceStatus = (Get-Service -Name $ServiceName -ErrorAction Stop).Status.ToString()
} catch {
    $serviceStatus = "Unknown"
}
if ($serviceStatus -eq "Running") {
    Write-Host "  service is already running; NSSM will handle its listener"
} elseif (-not $listeners) {
    Write-Host "  none"
}
if ($serviceStatus -ne "Running") {
    foreach ($listener in $listeners) {
        $pidValue = [int]$listener.OwningProcess
        if ($pidValue -le 0) {
            continue
        }
        Write-Host ("  stopping PID {0}" -f $pidValue)
        try {
            Stop-Process -Id $pidValue -Force -ErrorAction Stop
        } catch {
            Write-Host ("  could not stop PID {0}: {1}" -f $pidValue, $_.Exception.Message)
        }
    }
}

Write-Host ""
Write-Host "Restarting $ServiceName..."
& $Nssm restart $ServiceName *> $null
$health = Wait-ForHealth
if ($null -eq $health) {
    & $Nssm start $ServiceName *> $null
    $health = Wait-ForHealth
}

if ($null -eq $health) {
    Write-Host "ERROR: $ServiceName did not become healthy in time."
    sc.exe query $ServiceName
    exit 7
}

Wait-ForRunningState
Show-State
$health | Select-Object ok,elevated,service_account | ConvertTo-Json -Compress
Write-Host ""
Write-Host "Done. Dashboard: http://127.0.0.1:$Port/"
exit 0
