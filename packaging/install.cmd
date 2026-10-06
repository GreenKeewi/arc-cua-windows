@echo off
cd /d "%~dp0"
py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels "arc-cua[windows,browser]==0.1.2.dev1"
if errorlevel 1 exit /b 1
echo Installed. Run demo.cmd to test the Windows desktop, or mcp.cmd for an MCP client.
