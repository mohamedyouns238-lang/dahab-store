@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=
py -3 --version >nul 2>nul && set PY=py -3
if not defined PY python --version >nul 2>nul && set PY=python
if not defined PY (
  echo لازم تثبّت بايثون مرة واحدة ^(مجاني^). هيتفتح موقع التحميل...
  echo مهم: علّم على Add python.exe to PATH أثناء التثبيت، وبعدها شغّل run.bat تاني.
  start https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo تجهيز البرنامج لأول مرة ^(يحتاج إنترنت مرة واحدة فقط، دقيقة أو اتنين^)...
  %PY% -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo فشل التجهيز. اتأكد إن الإنترنت شغال وجرّب تاني.
    rmdir /s /q .venv
    pause
    exit /b 1
  )
)
start "" ".venv\Scripts\pythonw.exe" run_local.py
