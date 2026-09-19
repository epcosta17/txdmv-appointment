@echo off
REM TxDMV Appointment Desk - doble clic para abrir.
REM Necesita Python 3.10+ instalado (python.org, marca "Add to PATH").

cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo.
  echo   No se encontro Python.
  echo   Instalalo desde https://www.python.org/downloads/
  echo   y marca la casilla "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo Preparando el entorno por primera vez, esto tarda un minuto...
  py -m venv .venv
  if errorlevel 1 goto fail
  call .venv\Scripts\activate.bat
  py -m pip install --quiet --upgrade pip
  py -m pip install --quiet -r requirements.txt
  if errorlevel 1 goto fail
) else (
  call .venv\Scripts\activate.bat
)

echo.
echo   Abriendo Appointment Desk en el navegador...
echo   Deja esta ventana abierta. Cierrala para apagar la app.
echo.
py app.py
goto :eof

:fail
echo.
echo   Fallo la instalacion. Revisa tu conexion e intentalo de nuevo.
pause
exit /b 1
