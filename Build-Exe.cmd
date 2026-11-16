@echo off
rem Builds dist\Sentinel Workspace.exe. Azure CLI and GitHub CLI are still needed on each PC.
setlocal
pushd "%~dp0" || exit /b 1
py -m pip install --upgrade pyinstaller -r requirements.txt || goto failed
rem The icon and the Windows version resource are drawn from the app's own brand module and version.
rem LICENSE is bundled for About > Open license; UPX is off because packed exes trip antivirus heuristics.
py scripts\make_build_assets.py || goto failed
py -m PyInstaller --noconfirm --clean --onefile --windowed --name "Sentinel Workspace" ^
  --icon assets\sentinel-workspace.ico --version-file assets\version-info.txt ^
  --paths . --collect-submodules sentinel_app ^
  --add-data "docs;docs" --add-data "templates;templates" --add-data "LICENSE;." --noupx ^
  scripts\sentinel_exe.py || goto failed
echo.
echo Built: %CD%\dist\Sentinel Workspace.exe
rem The exe keeps its data in desktop-data beside it. Where this checkout has a desktop-data folder, the one
rem Start-Sentinel.cmd uses, a copy of the new exe goes beside it, so the exe opens the same clients and settings.
rem A copy that can't be made (the app is open) doesn't fail the build.
if exist "desktop-data\" (
  copy /Y "dist\Sentinel Workspace.exe" "Sentinel Workspace.exe" >nul 2>&1 && (
    echo Copied beside desktop-data: %CD%\Sentinel Workspace.exe
  ) || (
    echo Not copied beside desktop-data: close Sentinel Workspace, then copy
    echo dist\Sentinel Workspace.exe into %CD% yourself.
  )
)
popd
exit /b 0
:failed
popd
echo Build failed.
exit /b 1
