@echo off
rem Double-click to start AgentTape (in WSL) and open it in the browser.
rem Arguments pass through: AgentTape.cmd -Stop, -Status, -Setup, -InstallShortcut.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0AgentTape.ps1" %*
if errorlevel 1 pause
