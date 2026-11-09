@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"

set "MINIMUM_PYTHON_MAJOR=3"
set "MINIMUM_PYTHON_MINOR=9"

if "%~1"=="" exit /b 1
set "HOOK_SCRIPT=%~dp0tenetora_hook.py"

set "EXPLICIT_PYTHON=%TENETORA_PYTHON%"
if defined EXPLICIT_PYTHON (
  "%EXPLICIT_PYTHON%" -I -B -c "import sys; raise SystemExit(0 if sys.version_info >= (%MINIMUM_PYTHON_MAJOR%, %MINIMUM_PYTHON_MINOR%) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Tenetora hook launcher rejected TENETORA_PYTHON=%EXPLICIT_PYTHON%; expected an executable Python %MINIMUM_PYTHON_MAJOR%.%MINIMUM_PYTHON_MINOR% or newer. 1>&2
    exit /b 1
  )
  if "%~1"=="--check" (
    "%EXPLICIT_PYTHON%" -I -B "%HOOK_SCRIPT%" --runtime-check
    exit /b !ERRORLEVEL!
  )
  "%EXPLICIT_PYTHON%" -I -B "%HOOK_SCRIPT%" %*
  exit /b !ERRORLEVEL!
)

set "HARNESS_HOME=%TENETORA_HOME%"
if not defined HARNESS_HOME set "HARNESS_HOME=%USERPROFILE%\.tenetora"
set "RUNTIME_FILE=%TENETORA_RUNTIME_PYTHON_FILE%"
if not defined RUNTIME_FILE set "RUNTIME_FILE=%HARNESS_HOME%\runtime\python"
set "RUNTIME_PYTHON="
if exist "%RUNTIME_FILE%" set /p RUNTIME_PYTHON=<"%RUNTIME_FILE%"
if defined RUNTIME_PYTHON (
  "%RUNTIME_PYTHON%" -I -B -c "import sys; raise SystemExit(0 if sys.version_info >= (%MINIMUM_PYTHON_MAJOR%, %MINIMUM_PYTHON_MINOR%) else 1)" >nul 2>nul
  if not errorlevel 1 (
    if "%~1"=="--check" (
      "%RUNTIME_PYTHON%" -I -B "%HOOK_SCRIPT%" --runtime-check
      exit /b !ERRORLEVEL!
    )
    "%RUNTIME_PYTHON%" -I -B "%HOOK_SCRIPT%" %*
    exit /b !ERRORLEVEL!
  )
)

for %%P in ("%LocalAppData%\Programs\Python\Python313\python.exe" "%LocalAppData%\Programs\Python\Python312\python.exe" "%LocalAppData%\Programs\Python\Python311\python.exe" "%LocalAppData%\Programs\Python\Python310\python.exe" "%LocalAppData%\Programs\Python\Python39\python.exe") do (
  if exist "%%~P" (
    "%%~P" -I -B -c "import sys; raise SystemExit(0 if sys.version_info >= (%MINIMUM_PYTHON_MAJOR%, %MINIMUM_PYTHON_MINOR%) else 1)" >nul 2>nul
    if not errorlevel 1 (
      if "%~1"=="--check" (
        "%%~P" -I -B "%HOOK_SCRIPT%" --runtime-check
        exit /b !ERRORLEVEL!
      )
      "%%~P" -I -B "%HOOK_SCRIPT%" %*
      exit /b !ERRORLEVEL!
    )
  )
)

set "WHERE_EXE=%SystemRoot%\System32\where.exe"
if exist "%WHERE_EXE%" (
  for /f "delims=" %%I in ('"%WHERE_EXE%" py.exe 2^>nul') do (
    for %%V in (3.13 3.12 3.11 3.10 3.9) do (
      "%%I" -%%V -I -B -c "import sys; raise SystemExit(0 if sys.version_info >= (%MINIMUM_PYTHON_MAJOR%, %MINIMUM_PYTHON_MINOR%) else 1)" >nul 2>nul
      if not errorlevel 1 (
        if "%~1"=="--check" (
          "%%I" -%%V -I -B "%HOOK_SCRIPT%" --runtime-check
          exit /b !ERRORLEVEL!
        )
        "%%I" -%%V -I -B "%HOOK_SCRIPT%" %*
        exit /b !ERRORLEVEL!
      )
    )
  )

  for %%P in (python3.13 python3.12 python3.11 python3.10 python3.9 python3 python) do (
    for /f "delims=" %%I in ('"%WHERE_EXE%" %%P 2^>nul') do (
      "%%I" -I -B -c "import sys; raise SystemExit(0 if sys.version_info >= (%MINIMUM_PYTHON_MAJOR%, %MINIMUM_PYTHON_MINOR%) else 1)" >nul 2>nul
      if not errorlevel 1 (
        if "%~1"=="--check" (
          "%%I" -I -B "%HOOK_SCRIPT%" --runtime-check
          exit /b !ERRORLEVEL!
        )
        "%%I" -I -B "%HOOK_SCRIPT%" %*
        exit /b !ERRORLEVEL!
      )
    )
  )
)

echo Tenetora hooks require Python %MINIMUM_PYTHON_MAJOR%.%MINIMUM_PYTHON_MINOR% or newer. 1>&2
echo Managed runtime pointer: %RUNTIME_FILE% 1>&2
echo PATH: %PATH% 1>&2
echo Reinstall Tenetora or set TENETORA_PYTHON to a compatible absolute path. 1>&2
exit /b 1
