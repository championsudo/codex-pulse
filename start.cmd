@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
    if not errorlevel 1 goto run_venv
)
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run_py
python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run_python
python3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto run_python3
echo Python 3.10+ is required. Install Python or create .venv with Python 3.10+ in this folder. 1>&2
exit /b 1
:run_venv
".venv\Scripts\python.exe" "%~dp0server.py" --open %*
exit /b %errorlevel%
:run_py
py -3 "%~dp0server.py" --open %*
exit /b %errorlevel%
:run_python
python "%~dp0server.py" --open %*
exit /b %errorlevel%
:run_python3
python3 "%~dp0server.py" --open %*
exit /b %errorlevel%
