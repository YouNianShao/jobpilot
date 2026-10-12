' JobPilot 静默启动器：释放端口 -> 后台起服务 -> 打开看板（全程无窗口）
Option Explicit
Dim sh, fso, base, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "cmd /c """"" & base & "\start.bat"""""
sh.Run cmd, 0, False