@echo off
echo ================================================
echo   啟動 Chrome（Debug 模式）
echo ================================================
echo.
echo [注意] 請確保 Chrome 已完全關閉，否則 Debug Port 無法啟動。
echo.

:: 嘗試找 Chrome 路徑
set CHROME_PATH=
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
)
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
)

if "%CHROME_PATH%"=="" (
    echo [錯誤] 找不到 Chrome，請手動修改此檔案中的 CHROME_PATH
    pause
    exit /b 1
)

echo 啟動中：%CHROME_PATH%
echo.
start "" "%CHROME_PATH%" --remote-debugging-port=9222 --profile-directory="Default"

echo Chrome 已啟動（debug port: 9222）
echo.
echo 接下來：
echo   1. 在 Chrome 確認已登入 1688 / 淘寶
echo   2. 另開終端機執行 start_worker.bat
echo.
pause
