@echo off
rem Builds nearshare-ble.exe (the Windows BLE helper) with MSVC.
rem Needs Visual Studio 2022 Build Tools (C++ workload) + a Windows 10/11 SDK,
rem which ships the C++/WinRT headers. Run from any directory:
rem     windows\ble\build.cmd
setlocal
cd /d "%~dp0"

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto no_vs

set "VSROOT="
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT goto no_vs
if not exist "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" goto no_vs

call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 goto fail

rem /MT links the CRT statically so the exe runs without the VC++ redistributable.
cl /nologo /std:c++17 /EHsc /O2 /MT /permissive- /W4 /utf-8 nearshare-ble.cpp /link /OUT:nearshare-ble.exe WindowsApp.lib
if errorlevel 1 goto fail

del /q nearshare-ble.obj >nul 2>&1
echo Built %~dp0nearshare-ble.exe
exit /b 0

:no_vs
echo error: Visual Studio 2022 Build Tools with the C++ workload were not found 1>&2
exit /b 1

:fail
echo error: build failed 1>&2
exit /b 1
