@echo off
chcp 65001 >nul
set "SCRIPTDIR=%~dp0"
cd /d "%SCRIPTDIR%"

set PYCMD=
set PYFOUND=0

py -3 --version >nul 2>nul
if %errorlevel%==0 set PYFOUND=1
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if %errorlevel%==0 (
    set PYCMD=py -3
    goto :run
)

python --version >nul 2>nul
if %errorlevel%==0 set PYFOUND=1
python -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if %errorlevel%==0 (
    set PYCMD=python
    goto :run
)

echo.
if "%PYFOUND%"=="1" echo Se encontro Python, pero es anterior a la version 3.10.
if "%PYFOUND%"=="0" echo No se encontro un interprete de Python en este equipo.
echo Instala Python 3.10 o superior desde https://www.python.org/downloads/
echo (marca la casilla "Add python.exe to PATH" durante la instalacion)
echo y vuelve a ejecutar este archivo.
echo.
pause
exit /b 1

:run
%PYCMD% "%SCRIPTDIR%WoWLogExtractor.py" %*

pause
