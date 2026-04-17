# ============================================================
#  THE FORGE - Clean Launch Script
#  Kills stale processes, restarts Ollama + Forge server,
#  verifies API health, and opens the dashboard.
# ============================================================

$ErrorActionPreference = "SilentlyContinue"
$env:PYTHONUTF8 = "1"
$env:VITALIS_ISOLATED = "1"

$PORT = 8777
$OLLAMA_MODEL = "qwen2.5-coder:7b"

Write-Host ""
Write-Host "  ============================================" -ForegroundColor DarkYellow
Write-Host "   THE FORGE - Clean Launcher" -ForegroundColor Yellow
Write-Host "  ============================================" -ForegroundColor DarkYellow
Write-Host ""

# Step 1: Kill stale Forge server processes
Write-Host "  [1/6] Killing stale Python processes..." -ForegroundColor Cyan
Get-Process -Name python -ErrorAction SilentlyContinue | ForEach-Object {
    $cmdLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
    if ($cmdLine -like "*forge_server*" -or $cmdLine -like "*forge_desktop*") {
        Stop-Process -Id $_.Id -Force
        Write-Host "        Killed PID $($_.Id) (Forge)" -ForegroundColor DarkGray
    }
}
Start-Sleep -Seconds 1

# Step 2: Ensure Ollama is running
Write-Host "  [2/6] Checking Ollama service..." -ForegroundColor Cyan
$ollamaUp = $false
try {
    $r = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -eq 200) { $ollamaUp = $true }
} catch {}

if (-not $ollamaUp) {
    Write-Host "        Ollama not responding. Starting..." -ForegroundColor DarkGray
    Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 4
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ollamaUp = $true }
    } catch {}
}

if ($ollamaUp) {
    Write-Host "        Ollama is UP" -ForegroundColor Green
} else {
    Write-Host "        Ollama failed to start (local SLMs unavailable)" -ForegroundColor Yellow
}

# Step 3: Verify Qwen model is cached
Write-Host "  [3/6] Checking Qwen model ($OLLAMA_MODEL)..." -ForegroundColor Cyan
if ($ollamaUp) {
    try {
        $tags = (Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 3).Content | ConvertFrom-Json
        $qwenFound = $tags.models | Where-Object { $_.name -like "qwen2.5-coder*" }
        if ($qwenFound) {
            $size = [math]::Round($qwenFound[0].size / 1GB, 1)
            Write-Host "        $($qwenFound[0].name) cached (${size}GB)" -ForegroundColor Green
        } else {
            Write-Host "        Model not found. Run: ollama pull $OLLAMA_MODEL" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "        Could not query models" -ForegroundColor Yellow
    }
} else {
    Write-Host "        Skipped (Ollama not running)" -ForegroundColor DarkGray
}

# Step 4: Start Forge server & Desktop Integration
Write-Host "  [4/6] Starting Forge server & Desktop App..." -ForegroundColor Cyan

# Register context menus first
Start-Process -FilePath "python" -ArgumentList "c:\TheForge\forge_desktop.py --register" -WorkingDirectory "c:\TheForge" -WindowStyle Hidden -Wait

$serverProc = Start-Process -FilePath "python" -ArgumentList "c:\TheForge\forge_server.py" -WorkingDirectory "c:\TheForge" -WindowStyle Hidden -PassThru
Write-Host "        Server PID:  $($serverProc.Id)" -ForegroundColor DarkGray

$desktopProc = Start-Process -FilePath "python" -ArgumentList "c:\TheForge\forge_desktop.py --tray" -WorkingDirectory "c:\TheForge" -WindowStyle Hidden -PassThru
Write-Host "        Desktop PID: $($desktopProc.Id)" -ForegroundColor DarkGray

# Step 5: Wait for API health
Write-Host "  [5/6] Waiting for API health..." -ForegroundColor Cyan
$healthy = $false
for ($i = 1; $i -le 25; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$PORT/api/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) {
            $health = $r.Content | ConvertFrom-Json
            $healthy = $true
            break
        }
    } catch {}
    Write-Host "        Attempt $i/25..." -ForegroundColor DarkGray
}

if ($healthy) {
    $ollamaStatus = "Offline"
    if ($ollamaUp) { $ollamaStatus = "Online" }

    Write-Host ""
    Write-Host "  ============================================" -ForegroundColor Green
    Write-Host "   ALL SYSTEMS GO" -ForegroundColor Green
    Write-Host "  ============================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "   API Status:      $($health.status)" -ForegroundColor White
    Write-Host "   Vitalis:         $($health.vitalis)" -ForegroundColor White
    Write-Host "   Models:          $($health.models) providers" -ForegroundColor White
    $budgetVal = [math]::Round($health.budget_remaining, 2)
    Write-Host "   Budget:          $budgetVal remaining" -ForegroundColor White
    Write-Host "   Provenance:      $($health.provenance_entries) entries" -ForegroundColor White
    Write-Host "   Circuit Breaker: $($health.circuit_breaker)" -ForegroundColor White
    $uptimeVal = [math]::Round($health.uptime_s, 1)
    Write-Host "   Uptime:          ${uptimeVal}s" -ForegroundColor White
    Write-Host "   Ollama:          $ollamaStatus" -ForegroundColor White
    Write-Host ""
    Write-Host "   Dashboard:  http://localhost:$PORT/" -ForegroundColor Yellow
    Write-Host "   API:        http://localhost:$PORT/api/health" -ForegroundColor Yellow
    Write-Host ""
} else {
    Write-Host ""
    Write-Host "   FAILED - Server did not respond after 25 attempts" -ForegroundColor Red
    Write-Host "   Check logs: python c:\TheForge\forge_server.py" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# Step 6: Open Dashboard
Write-Host "  [6/6] Opening dashboard..." -ForegroundColor Cyan
Start-Process "http://localhost:$PORT"
Write-Host "        Done!" -ForegroundColor Green
Write-Host ""
