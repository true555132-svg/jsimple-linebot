@echo off
echo ================================================
echo   J SIMPLE 本機商品爬取 Worker
echo ================================================
echo.
echo 確保已執行 start_chrome_debug.bat 並登入 1688 / 淘寶
echo.

:: 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python，請確認已安裝
    pause
    exit /b 1
)

:: 檢查 playwright
python -c "import playwright" >nul 2>&1
if errorlevel 1 (
    echo [安裝] 安裝 playwright...
    pip install playwright requests
    playwright install
)

echo 啟動 Worker...
echo.
python local_worker.py
echo.
pause
