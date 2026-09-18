@echo off
chcp 65001 >nul
echo ==========================================================
echo   正在關閉 飛書 Obsidian 影片知識庫機器人...
echo ==========================================================
powershell -NoProfile -Command "Get-WmiObject Win32_Process -Filter \"name = 'python.exe'\" | Where-Object { $_.CommandLine -match 'feishu_bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('[OK] 已停止機器人進程 PID: ' + $_.ProcessId) -ForegroundColor Green }"
echo 機器人服務已成功關閉。
pause
