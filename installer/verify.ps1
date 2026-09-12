# ASCII-only acceptance test v2: better probes + captured engine logs.
$ErrorActionPreference = 'Continue'
$progs   = Join-Path $env:LOCALAPPDATA 'Programs'
$logDir  = 'C:\ProgramMine\a4agent\installer\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$model9b = 'C:\models\Ornith-1.5-9B\Ornith-1.5-9B-Q4_K_M.gguf'

function Probe {
    param([int]$Port)
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-LiveEngine {
    param([string]$Label, [string]$EngineExe, [int]$Port)
    if (-not (Test-Path $EngineExe)) { Write-Host "[LIVE] $Label -> engine missing"; return }

    # sanity: does the binary even print its version?
    $verOut = & $EngineExe --version 2>&1 | Out-String
    Write-Host ("[LIVE] {0} --version => {1}" -f $Label, ($verOut.Trim() -replace "`r`n", ' | '))

    $outLog = Join-Path $logDir "$Label.out.log"
    $errLog = Join-Path $logDir "$Label.err.log"
    Write-Host "[LIVE] $Label starting on :$Port ..."
    $p = Start-Process $EngineExe -ArgumentList `
        '-m', $model9b, '--host', '127.0.0.1', '--port', "$Port", `
        '-ngl', '99', '-c', '2048', '-fa', 'on' -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    $ready = $false; $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 300 -and -not $p.HasExited) {
        if (Probe $Port) { $ready = $true; break }
        Start-Sleep 2
    }
    if ($ready) {
        Write-Host ("[LIVE] {0} -> RUNNING OK ({1:N0}s)" -f $Label, $sw.Elapsed.TotalSeconds)
    }
    elseif ($p.HasExited) {
        Write-Host "[LIVE] $Label -> EXITED code $($p.ExitCode)"
        Write-Host ("[LIVE] stderr tail: " + ((Get-Content $errLog -Tail 8) -join ' / '))
    }
    else {
        Write-Host "[LIVE] $Label -> NOT READY in 300s"
        Write-Host ("[LIVE] stdout tail: " + ((Get-Content $outLog -Tail 10) -join ' / '))
        Write-Host ("[LIVE] stderr tail: " + ((Get-Content $errLog -Tail 10) -join ' / '))
    }
    try { if (-not $p.HasExited) { $p.Kill($true); $p.WaitForExit(8000) } } catch {}
}

Test-LiveEngine -Label 'cu13.3' -EngineExe (Join-Path $progs 'a4agent-cu13.3\engine\llama-server.exe') -Port 8098
Test-LiveEngine -Label 'vulkan' -EngineExe (Join-Path $progs 'a4agent-vulkan\engine\llama-server.exe')  -Port 8097

Write-Host '===== ACCEPTANCE V2 DONE ====='
