@echo off
REM ===========================================================================
REM  run_dev.bat — one-click dev launcher for YouTube Transcript Extractor
REM
REM  Creates (or reuses) a .venv at the project root, installs the full
REM  dependency set (core + AI chat + STT fallback), then launches the GUI via
REM  `python run.py`. Double-click or run from any terminal.
REM
REM  - Core (자막/MP3/MP4):        requirements.txt          -> always
REM  - AI chat (내장 모델):         requirements-ai.txt       -> attempted
REM        llama-cpp-python may need an AVX2 source build; see that file.
REM        A wheel-install failure here is non-fatal — AI chat just stays off.
REM  - STT fallback (faster-whisper): requirements-stt.txt    -> attempted
REM        Also non-fatal; STT options auto-disable if the import fails.
REM
REM  Re-run safely: pip skips already-installed packages, so the second launch
REM  is fast. Pass any args through to the app, e.g. `run_dev.bat --selftest`.
REM
REM  Long paths: llama-cpp-python vendors a deeply-nested llama.cpp tree that
REM  exceeds MAX_PATH (260) on default Windows. Python 3.6+ installers already
REM  declare longPathAware in their manifest, so the ONLY thing needed is the
REM  system-wide registry flip — run enable_long_paths.bat once. Without it
REM  the llama-cpp-python install fails with "No such file or directory" on a
REM  .svelte path. This script warns when that registry value is missing.
REM ===========================================================================

setlocal enableextensions

REM UTF-8 codepage so the Korean status messages below render correctly in the
REM console window. Without this, cmd's default cp949 mangles them.
chcp 65001 >nul

cd /d "%~dp0"

echo.
echo === YouTube Transcript Extractor - dev launcher ===
echo.

REM --- Long path check -------------------------------------------------------
REM llama-cpp-python's vendored llama.cpp tree has Svelte paths > 260 chars.
REM We read HKLM\...\FileSystem\LongPathsEnabled; absence or 0 means disabled.
REM reg query returns errorlevel 1 when the value is missing, so we treat any
REM non-"0x1" result as "needs enabling" and steer the user to the helper.
set "LONG_PATHS_OK="
for /f "tokens=3" %%v in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled 2^>nul') do (
    set "LONG_PATHS_VAL=%%v"
)
if "%LONG_PATHS_VAL%"=="0x1" (
    set "LONG_PATHS_OK=1"
)
if not defined LONG_PATHS_OK (
    echo [WARN] Windows Long Path 가 비활성화되어 있습니다.
    echo        llama-cpp-python 설치가 실패할 수 있습니다 ^(260자 경로 제한^).
    echo        동봉된 enable_long_paths.bat 을 한 번 실행하세요 ^(UAC 권한 필요^).
    echo        이 경고를 무시하고 계속 진행합니다...
    echo.
)

REM --- Pick a Python interpreter -------------------------------------------
REM Prefer the `py` launcher (resolves to the newest installed Python), then
REM fall back to `python` on PATH. Bail out with an actionable message if
REM neither exists — a bare "'python' is not recognized" is unhelpful.
where py >nul 2>nul
if %errorlevel%==0 (
    set "PYCMD=py"
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set "PYCMD=python"
    ) else (
        echo [ERR] Python을 찾을 수 없습니다.
        echo       https://www.python.org/downloads/ 에서 Python 3.10+ 설치 후 다시 시도하세요.
        echo.
        pause
        exit /b 1
    )
)

REM --- Create .venv once ----------------------------------------------------
REM The venv is reused across runs; only the very first launch pays the create
REM cost. We touch a marker file so a half-created venv (interrupted) is
REM detected and rebuilt instead of crashing on the activate step.
if not exist ".venv\Scripts\python.exe" (
    echo [1/4] 가상환경 생성 중 (.venv^)...
    %PYCMD% -m venv .venv
    if errorlevel 1 (
        echo [ERR] 가상환경 생성 실패.
        pause
        exit /b 1
    )
) else (
    echo [1/4] 기존 .venv 재사용.
)

REM Use the venv's python directly; `call .venv\Scripts\activate` would also
REM work but leaves env vars leaking into the parent shell on some systems.
set "VENV_PY=.venv\Scripts\python.exe"

REM --- Upgrade pip ----------------------------------------------------------
echo [2/4] pip 업그레이드 중...
"%VENV_PY%" -m pip install --upgrade pip >nul
if errorlevel 1 (
    echo [WARN] pip 업그레이드 실패 - 계속 진행합니다.
)

REM --- Core dependencies (자막 / MP3 / MP4) --------------------------------
echo [3/4] 핵심 의존성 설치 중 (requirements.txt^)...
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERR] 핵심 의존성 설치 실패. 위 로그를 확인하세요.
    pause
    exit /b 1
)

REM --- Optional: AI chat (llama-cpp-python) --------------------------------
echo [4/4] 추가 기능 의존성 설치 중 (AI + STT, 실패 시 건너뜀^)...
"%VENV_PY%" -m pip install -r requirements-ai.txt
if errorlevel 1 (
    echo [WARN] llama-cpp-python 설치 실패 - AI 채팅은 비활성화됩니다.
    echo       원인1: Long Path 비활성화 ^(260자 경로 제한^) -> enable_long_paths.bat 실행
    echo       원인2: AVX2 소스 빌드 필요 -> requirements-ai.txt 참고
) else (
    echo [OK]  llama-cpp-python 설치 완료 (AI 채팅 사용 가능^)
)

REM --- Optional: STT (faster-whisper) --------------------------------------
"%VENV_PY%" -m pip install -r requirements-stt.txt
if errorlevel 1 (
    echo [WARN] faster-whisper 설치 실패 - STT 폴백은 비활성화됩니다.
    echo       requirements-stt.txt 참고.
) else (
    echo [OK]  faster-whisper 설치 완료 (STT 폴백 사용 가능^)
)

REM --- Launch --------------------------------------------------------------
echo.
echo === 앱 실행 중 (python run.py %*^) ===
echo.
"%VENV_PY%" run.py %*
set "EXITCODE=%errorlevel%"

REM If launched by double-click (no args), keep the window open so a crash
REM traceback doesn't vanish instantly. When called with args (e.g. from a
REM terminal running --selftest), don't pause — the caller owns the window.
if "%~1"=="" (
    echo.
    echo === 앱이 종료되었습니다 (exit %EXITCODE%^) ===
    pause
)

endlocal & exit /b %EXITCODE%
