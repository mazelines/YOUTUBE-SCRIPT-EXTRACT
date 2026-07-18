@echo off
REM ===========================================================================
REM  enable_long_paths.bat — one-time Windows Long Path activation
REM
REM  pip fails installing llama-cpp-python because the vendored llama.cpp
REM  tree contains deeply-nested Svelte paths (e.g.
REM  .../ChatAttachmentsListItemMcpPrompt.svelte) that exceed MAX_PATH (260).
REM  Enabling LongPathsEnabled=1 in the registry lifts that cap system-wide,
REM  with no reboot required.
REM
REM  This script MUST run elevated (HKLM write). It re-launches itself with
REM  admin rights via UAC if not already elevated, so a double-click is all
REM  that's needed.
REM
REM  After this runs once, run_dev.bat picks up long-path support automatically
REM  and llama-cpp-python installs cleanly.
REM ===========================================================================

setlocal enableextensions
chcp 65001 >nul

REM --- Self-elevate if not admin ------------------------------------------------
REM `net session` succeeds only with admin rights; cheap and side-effect free.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] 관리자 권한이 필요합니다. UAC 프롬프트에서 '예'를 눌러주세요.
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo === Windows Long Path 활성화 ===
echo.

REM --- Set the registry value ---------------------------------------------------
REM LongPathsEnabled (DWORD) = 1 tells the kernel to accept paths > 260 chars
REM for opt-in applications (Python/pip opt in via their manifest). No reboot
REM needed — the change takes effect for new processes immediately.
reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" ^
    /v LongPathsEnabled /t REG_DWORD /d 1 /f
if errorlevel 1 (
    echo [ERR] 레지스트리 설정 실패.
    pause
    exit /b 1
)

REM --- Verify ------------------------------------------------------------------
reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled

echo.
echo [OK] Long Path 가 활성화되었습니다.
echo      이제 run_dev.bat 을 다시 실행하면 llama-cpp-python 설치가 정상 동작합니다.
echo      (재부팅 불필요, 새 프로세스부터 즉시 적용)
echo.
pause
endlocal
