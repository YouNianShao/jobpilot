# 起停一个「只看界面」用的临时 Chrome（独立 profile + 独立端口）。
#
# 为什么要它：前端迭代要反复截图和跑 tools\check_ui.py，而用户那个调试 Chrome
# 带着登录态，不该拿来当试验场；它被关掉或状态异常时也不该卡住整个流程。
# 用法（不带参数 = 先清理旧实例再起一个新的）:
#   powershell -File tools\ui_chrome.ps1
#   powershell -File tools\ui_chrome.ps1 -Stop
# 截图 / 自检时把 CDP_PORT 指到这个端口即可：$env:CDP_PORT = "9333"
param([switch]$Stop, [int]$Port = 9333)
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$marker = Join-Path $root "data\.ui-chrome.txt"
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"

if (Test-Path $marker) {
  $old = (Get-Content -Encoding ascii $marker | Select-Object -First 1).Trim()
  $alive = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like ("*" + $old + "*") })
  foreach ($proc in $alive) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
  $resolved = [IO.Path]::GetFullPath($old)
  $tmp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
  if ($resolved.StartsWith($tmp) -and (Split-Path $resolved -Leaf) -like "jp2ui_*") {
    Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
  Write-Host ("旧实例已清理（进程 " + $alive.Count + " 个）")
}
if ($Stop) { return }

$dir = Join-Path ([IO.Path]::GetTempPath()) ("jp2ui_" + (Get-Random))
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Start-Process -FilePath $chrome -WindowStyle Hidden -ArgumentList @(
  "--remote-debugging-port=$Port", "--user-data-dir=$dir", "--headless=new",
  "--no-first-run", "--no-default-browser-check", "--window-size=1600,1200", "about:blank"
) | Out-Null
Start-Sleep -Seconds 5
[IO.File]::WriteAllText($marker, $dir)
Write-Host ("临时 Chrome 已启动: http://127.0.0.1:" + $Port + "  配置目录: " + $dir)
