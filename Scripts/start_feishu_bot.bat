@echo off
chcp 65001 >nul
title 飛書 Obsidian 影片知識庫機器人服務

echo ==========================================================
echo   飛書 Obsidian 影片知識庫機器人 (WebSocket 長連接版)
echo   正在啟動，請保持此視窗開啟...
echo ==========================================================

REM 切換至專案根目錄
cd /d "%~dp0.."

REM 代理設定（Gemini 走代理，飛書國內長連接不走代理）
set HTTP_PROXY=http://127.0.0.1:63355
set HTTPS_PROXY=http://127.0.0.1:63355
set NO_PROXY=127.0.0.1,localhost,*.feishu.cn,*.larksuite.com,feishu.cn,larksuite.com

REM 直接指定 feishu_bot.py 的絕對路徑，徹底防止路徑重疊
uv run --python 3.14 --with lark-oapi --with requests --with yt-dlp python "%~dp0feishu_bot.py"
pause

