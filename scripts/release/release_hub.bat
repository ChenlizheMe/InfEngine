@echo off
setlocal EnableExtensions
cd /d "%~dp0\..\.."

rem Maintainer release commands:
rem   scripts\release\release_hub.bat 0.4.0                 Build and publish.
rem   scripts\release\release_hub.bat 0.4.0 --force         Replace a release.
rem   scripts\release\release_hub.bat 0.4.0 --overwrite     Alias for --force.
rem   scripts\release\release_hub.bat 0.4.0 --build-only    Build without upload.
rem   scripts\release\release_hub.bat 0.4.0 --upload-only   Upload an existing build.

set "VERSION=%~1"
if not defined VERSION set /p "VERSION=Infernux version (for example 0.2.2): "
if not defined VERSION (
    echo [ERROR] A version number is required.
    exit /b 2
)

where conda >nul 2>nul
if not errorlevel 1 (
    call conda activate infernux
) else if exist "%ProgramData%\anaconda3\condabin\conda.bat" (
    call "%ProgramData%\anaconda3\condabin\conda.bat" activate infernux
) else if exist "%UserProfile%\anaconda3\condabin\conda.bat" (
    call "%UserProfile%\anaconda3\condabin\conda.bat" activate infernux
) else if exist "%UserProfile%\miniconda3\condabin\conda.bat" (
    call "%UserProfile%\miniconda3\condabin\conda.bat" activate infernux
) else (
    echo [ERROR] Conda is not available. Install or initialize Conda first.
    exit /b 3
)
if errorlevel 1 (
    echo [ERROR] Failed to activate the infernux Conda environment.
    exit /b 4
)

set "PUBLISH_ARG=-Publish"
set "FORCE_ARG="
set "UPLOAD_ONLY_ARG="
for %%A in (%*) do (
    if /I "%%~A"=="--build-only" set "PUBLISH_ARG="
    if /I "%%~A"=="--force" set "FORCE_ARG=-Force"
    if /I "%%~A"=="--overwrite" set "FORCE_ARG=-Force"
    if /I "%%~A"=="--upload-only" set "UPLOAD_ONLY_ARG=-UploadOnly"
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release_hub.ps1" -Version "%VERSION%" %PUBLISH_ARG% %FORCE_ARG% %UPLOAD_ONLY_ARG%
exit /b %ERRORLEVEL%
