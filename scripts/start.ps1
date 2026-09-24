$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# Node 运行时预检:版本不够或构建未启用 TypeScript 支持时给出指引,而不是 ERR_UNKNOWN_FILE_EXTENSION(#38)
& node (Join-Path $root "scripts\check-node.mjs")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$openBrowser = $args -notcontains "--no-open"
$listening = Get-NetTCPConnection -LocalPort 8765,5930,8766 -State Listen -ErrorAction SilentlyContinue
if (($listening | Where-Object LocalPort -eq 8765) -and ($listening | Where-Object LocalPort -eq 5930) -and ($listening | Where-Object LocalPort -eq 8766)) {
  Write-Output "vibe-research already http://127.0.0.1:5930"
  if ($openBrowser) { Start-Process "http://127.0.0.1:5930" }
  exit 0
}
$api = $null
$ui = $null
$market = $null
function Stop-ProcessTree($Process) {
  if (-not $Process) { return }
  $rootId = [int]$Process.Id
  $all = @(Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object ProcessId, ParentProcessId)
  $pending = [System.Collections.Generic.Queue[int]]::new()
  $seen = [System.Collections.Generic.HashSet[int]]::new()
  $descendants = [System.Collections.Generic.List[int]]::new()
  $pending.Enqueue($rootId)
  [void]$seen.Add($rootId)
  while ($pending.Count -gt 0) {
    $parentId = $pending.Dequeue()
    foreach ($child in $all) {
      $childId = [int]$child.ProcessId
      if ([int]$child.ParentProcessId -eq $parentId -and $seen.Add($childId)) {
        $descendants.Add($childId)
        $pending.Enqueue($childId)
      }
    }
  }
  # 父进程已退出时 taskkill /PID <parent> /T 找不到树根；
  # CIM 快照里的 ParentProcessId 仍能让我们定位遗留子孙。
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  foreach ($targetId in @($descendants | Sort-Object -Descending)) {
    & taskkill.exe /PID $targetId /T /F *> $null
  }
  $Process.Refresh()
  if (-not $Process.HasExited) {
    & taskkill.exe /PID $rootId /T /F *> $null
  }
  $ErrorActionPreference = $prev
}
try {
  $marketPy = Join-Path $root "market-api\.venv\Scripts\python.exe"
  if (-not (Test-Path -LiteralPath $marketPy)) {
    throw "未找到题材行情服务解释器（market-api\.venv）。请先完成 market-api 依赖安装。"
  }
  $marketDb = Join-Path $root "market-api\zzquant.db"
  if (-not (Test-Path -LiteralPath $marketDb)) {
    throw "未找到题材行情库 market-api\zzquant.db。"
  }
  # 题材轮动只读 API：随产品一同启停；浏览器只打同源 /v3，不直连端口。
  # 校准源仅在本地驱动消息为空时回源补库（见 plate_reasons）。
  $marketCmd = @"
`$env:ZZQUANT_ENABLE_ORIGIN_REFERENCE = '1'
`$env:ZZQUANT_COLLECTOR_MODE = 'external'
& '$marketPy' -m uvicorn app.main:app --host 127.0.0.1 --port 8766
"@
  $market = Start-Process -FilePath powershell.exe -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $marketCmd
  ) -WorkingDirectory (Join-Path $root "market-api") -PassThru -NoNewWindow

  $api = Start-Process -FilePath node -ArgumentList @("orchestrator\src\api.ts", "--port", "8765", "--host", "127.0.0.1") -WorkingDirectory $root -PassThru -NoNewWindow
  # UI 由 Vite 按 VRA_LAN 决定绑定；默认回环，API 的回环绑定不变。
  $ui = Start-Process -FilePath npm.cmd -ArgumentList @("run", "dev", "--prefix", "desktop") -WorkingDirectory $root -PassThru -NoNewWindow
  $ready = $false
  $readyDeadline = [DateTime]::UtcNow.AddSeconds(60)
  while ([DateTime]::UtcNow -lt $readyDeadline) {
    $api.Refresh()
    $ui.Refresh()
    $market.Refresh()
    if ($market.HasExited) { throw "题材行情服务启动失败（退出码 $($market.ExitCode)），请检查 8766 端口与 market-api。" }
    if ($api.HasExited) { throw "API 启动失败（退出码 $($api.ExitCode)），请检查 8765 端口和产品配置。" }
    if ($ui.HasExited) { throw "界面启动失败（退出码 $($ui.ExitCode)）。" }
    # 经界面代理验证后端鉴权；令牌仍只留在本地服务，不传到浏览器或命令参数。
    & node (Join-Path $root "orchestrator\src\startup_health.ts")
    $coreOk = ($LASTEXITCODE -eq 0)
    $marketOk = $false
    try {
      $mr = Invoke-WebRequest -Uri "http://127.0.0.1:8766/v3/market/plates/17/rank/columns?days=1&limit=1" -UseBasicParsing -TimeoutSec 2
      $marketOk = ($mr.StatusCode -eq 200)
    } catch { $marketOk = $false }
    if ($coreOk -and $marketOk) { $ready = $true; break }
    Start-Sleep -Milliseconds 250
  }
  if (-not $ready) { throw "启动等待超过 60 秒。请检查上方日志、8765/5930/8766 端口以及产品配置；浏览器尚未打开。" }
  $api.Refresh()
  $ui.Refresh()
  $market.Refresh()
  if ($market.HasExited) { throw "题材行情服务启动失败（退出码 $($market.ExitCode)），请检查 8766 端口与 market-api。" }
  if ($api.HasExited) { throw "API 启动失败（退出码 $($api.ExitCode)），请检查 8765 端口和产品配置。" }
  if ($ui.HasExited) { throw "界面启动失败（退出码 $($ui.ExitCode)）。" }
  Write-Output "vibe-research ready http://127.0.0.1:5930"
  if ($openBrowser) { Start-Process "http://127.0.0.1:5930" }
  while (-not $api.HasExited -and -not $ui.HasExited -and -not $market.HasExited) {
    Start-Sleep -Milliseconds 250
    $api.Refresh()
    $ui.Refresh()
    $market.Refresh()
  }
  if ($market.HasExited) { throw "题材行情服务已停止（退出码 $($market.ExitCode)），界面与 API 同步关闭。" }
  if ($api.HasExited) { throw "API 已停止（退出码 $($api.ExitCode)），界面同步关闭。" }
  if ($ui.HasExited) { throw "界面已提前停止（退出码 $($ui.ExitCode)）。" }
} finally {
  Stop-ProcessTree $ui
  Stop-ProcessTree $api
  Stop-ProcessTree $market
}
