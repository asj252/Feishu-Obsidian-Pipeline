Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  飛書 Obsidian 影片知識庫機器人 (WebSocket 長連接版)" -ForegroundColor Green
Write-Host "  正在啟動，請保持此視窗開啟..." -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Cyan

Set-Location -Path "$PSScriptRoot\.."
$env:HTTP_PROXY = "http://127.0.0.1:63355"
$env:HTTPS_PROXY = "http://127.0.0.1:63355"
$env:NO_PROXY = "127.0.0.1,localhost,*.feishu.cn,*.larksuite.com,feishu.cn,larksuite.com"

uv run --python 3.14 --with lark-oapi --with requests --with yt-dlp python "$PSScriptRoot\feishu_bot.py"

