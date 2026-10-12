<#
静默启动看板。

为什么是 PowerShell 而不是 bat：原来那个 bat 里塞了带管道的 powershell -Command，
cmd 在部分情况下会把管道符切开，于是"停旧实例"这一步其实一直在静默失败；
更糟的是 pythonw 启动失败时错误没有任何地方可看，表现就是"双击了但打不开"。

两个关键细节：
1. 本文件必须存成「UTF-8 带 BOM」——Windows PowerShell 5.1 读没有 BOM 的 .ps1
   会按系统代码页解码，中文全变乱码（连报告文件名都会写错）。
2. 用 Win32_Process.Create 起服务，而不是 Start-Process：后者一旦带
   -RedirectStandardOutput 就会让调用方**一直挂到服务退出**（隐藏窗口会一直挂着），
   而且子进程会继承调用方的句柄。Create 出来的进程父进程是 WmiPrvSE，
   不继承句柄、也不阻塞。
3. 直接起 pythonw，日志由进程自己写（serve --log）。以前中间垫了一层
   cmd.exe /c "pythonw ... >> serve.log"：cmd 是控制台程序，WMI 起它时会弹出黑框，
   而且它得一直活着替 pythonw 转写日志 —— 服务跑多久，桌面上就挂多久一个 cmd 窗口。

用法: powershell -File tools\launch.ps1 [-Port 8700] [-WaitSeconds 12] [-Quiet]
#>
param(
  [int]$Port = 8700,
  [int]$WaitSeconds = 12,
  [switch]$Quiet          # 不开浏览器、不弹记事本（自动化测试用）
)

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $root
$data = Join-Path $root 'data'
New-Item -ItemType Directory -Force -Path $data | Out-Null
# stdout / stderr 都由服务进程自己写进这一个文件（serve --log）；
# 以前还有一个 serve.err.log，是 cmd 重定向 2>> 的产物，现在不会再有内容了
$outLog = Join-Path $data 'serve.log'
$launchMark = '==== 启动 '
# 报告文件名用 ASCII：避免不同 shell 的代码页差异把中文路径写坏
$report = Join-Path $data 'launch-failed.txt'

function Write-Report([string[]]$extra) {
  $lines = @("JobPilot 启动失败 $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
             "端口: $Port",
             "命令: $cmdline",
             "") + $extra
  # 只附「这一次启动」写下的日志：从最后一个启动标记往后取，免得把上次的旧报错当成这次的
  $lines += @("", "== serve.log：这次启动之后的输出（报错和 traceback 都在这里）==")
  if (Test-Path $outLog) {
    $all = @(Get-Content $outLog -Encoding UTF8)
    $from = 0
    for ($i = $all.Count - 1; $i -ge 0; $i--) { if ($all[$i].StartsWith($launchMark)) { $from = $i; break } }
    $tail = @($all[$from..($all.Count - 1)])
    if ($tail.Count -gt 80) { $tail = $tail[($tail.Count - 80)..($tail.Count - 1)] }
    if ($tail.Count -le 1) { $tail += '(这次启动之后服务一行都没写：多半是还没来得及接管日志就退出了，或者根本没起来)' }
    $lines += $tail
  } else { $lines += '(没有 serve.log)' }
  $lines | Set-Content -Encoding utf8 $report
  Write-Host "报告: $report"
  if (-not $Quiet) { Start-Process notepad $report }
}

Write-Host "[1/4] 停掉正在运行的 JobPilot ..."
$old = @(Get-CimInstance Win32_Process |
  Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*jobpilot.cli*' })
foreach ($proc in $old) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
if ($old.Count -gt 0) { Start-Sleep -Seconds 1 }

Write-Host "[2/4] 静默启动看板（日志 -> data\serve.log）..."
if ((Test-Path $outLog) -and ((Get-Item $outLog).Length -gt 512KB)) { Remove-Item $outLog -Force }
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$cmdline = '(还没拼出来)'
if (-not (Test-Path $pythonw)) {
  Write-Report @("找不到虚拟环境解释器：$pythonw", "先按 README 里的「快速开始」建好 .venv 再启动。")
  exit 1
}
# pythonw 是 GUI 子系统程序：不分配控制台，也就不会有任何窗口。
# 先写一行启动标记：万一进程在接管日志之前就挂了，报告里能看出「这次启动之后一行都没写」，
# 而不是把上一次的旧报错当成这次的。
Add-Content -Path $outLog -Encoding UTF8 -Value ("`n" + $launchMark + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " ====")
$cmdline = '"' + $pythonw + '" -m jobpilot.cli serve --port ' + $Port + ' --log "' + $outLog + '"'
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create `
  -Arguments @{ CommandLine = $cmdline; CurrentDirectory = $root }
if ($created.ReturnValue -ne 0) {
  Write-Report @("Win32_Process.Create 返回 $($created.ReturnValue)（没能启动进程）")
  exit 1
}

Write-Host "[3/4] 等看板起来（最多 $WaitSeconds 秒）..."
$url = "http://127.0.0.1:$Port"
$ok = $false
$tries = [Math]::Max(1, $WaitSeconds * 2)
# 探测 /api/status（不碰浏览器）。以前探的是 /api/health —— 那个接口要去连 Chrome，
# 浏览器状态异常时会卡住几十秒，于是服务明明起来了却被判成启动失败。
for ($i = 0; $i -lt $tries; $i++) {
  try { Invoke-WebRequest -UseBasicParsing "$url/api/status" -TimeoutSec 2 | Out-Null; $ok = $true; break }
  catch { Start-Sleep -Milliseconds 500 }
}

if ($ok) {
  Write-Host "[4/4] 打开 $url"
  if (-not $Quiet) { Start-Process $url }
  exit 0
}

Write-Host "[4/4] *** 启动失败 ***"
Write-Report @("进程 pid=$($created.ProcessId) 起来了，但 $WaitSeconds 秒内没能响应 $url/api/status")
exit 1