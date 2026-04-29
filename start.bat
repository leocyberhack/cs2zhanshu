@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%APP_ACCESS_KEY%"=="" set "APP_ACCESS_KEY=local-dev-key"
echo 启动 CS2 战术本...
echo 本地测试密钥: %APP_ACCESS_KEY%
python app.py
pause
