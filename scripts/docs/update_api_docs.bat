@echo off
setlocal EnableExtensions

rem Incrementally regenerate Infernux API Markdown and every derived website artifact.
rem The Python generator preserves USER CONTENT blocks, skips byte-identical files,
rem creates missing symbol pages, and deletes stale pages for removed symbols.

cd /d "%~dp0\..\.."

where conda >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Conda was not found. Install or initialize Conda, then retry.
    exit /b 1
)

call conda activate infernux
if errorlevel 1 (
    echo [ERROR] Could not activate the required Conda environment: infernux
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

echo [1/9] Updating API Markdown...
python docs\wiki\generate_api_docs.py %*
if errorlevel 1 goto :failed

echo [2/9] Restoring deterministic Wiki navigation...
node docs\tools\normalize-wiki-config.mjs
if errorlevel 1 goto :failed

echo [3/9] Applying curated API guidance...
node docs\tools\apply-api-curation.mjs
if errorlevel 1 goto :failed

echo [4/9] Updating the API index...
node docs\tools\build-api-index.mjs
if errorlevel 1 goto :failed

echo [5/9] Building the static Wiki...
python -m mkdocs build --strict --clean -f docs\wiki\mkdocs.yml
if errorlevel 1 goto :failed

echo [6/9] Optimizing generated Wiki output...
node docs\tools\optimize-static-site.mjs
if errorlevel 1 goto :failed

echo [7/9] Updating the unified sitemap...
node docs\tools\build-sitemap.mjs
if errorlevel 1 goto :failed

echo [8/9] Updating the offline application shell...
node docs\tools\build-service-worker.mjs
if errorlevel 1 goto :failed

echo [9/9] Verifying the generated website contracts...
node docs\tools\verify-site.mjs
if errorlevel 1 goto :failed

echo.
echo [OK] API documentation and derived website artifacts are current.
echo [NOTE] API release snapshots and version diffs are intentionally not modified.
exit /b 0

:failed
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" set "EXIT_CODE=1"
echo.
echo [ERROR] API documentation update stopped with exit code %EXIT_CODE%.
exit /b %EXIT_CODE%
