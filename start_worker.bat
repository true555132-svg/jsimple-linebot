@echo off
echo ================================================
echo   J SIMPLE 本機商品爬取 Worker v2
echo ================================================
echo.

:: 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python
    pause & exit /b 1
)

:: 檢查套件
python -c "import playwright, requests, rembg, PIL" >nul 2>&1
if errorlevel 1 (
    echo [安裝] 安裝必要套件...
    pip install playwright requests rembg pillow
    playwright install chromium
)

:: 第一次使用：有 --login 參數
if "%1"=="--login" (
    echo 【登入模式】請在瀏覽器中登入 1688 / 淘寶
    echo.
    python local_worker.py --login
    pause
    exit /b 0
)

:: 檢查是否已登入
if not exist "%USERPROFILE%\jsimple-worker-profile" (
    echo [提示] 尚未登入，請先執行：
    echo   start_worker.bat --login
    echo.
    pause & exit /b 0
)

echo 啟動 Worker（Ctrl+C 停止）...
echo.
python local_worker.py
pause
