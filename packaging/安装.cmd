@echo off
rem Windows installer helper: double-click it. The text and the logic are in install.ps1 (UTF-8, Chinese):
rem cmd.exe mis-reads non-ASCII text in a batch file, so this file stays ASCII and only starts the script.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
