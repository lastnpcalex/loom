@echo off
cd /d "C:\Users\exast\OneDrive\Documents\LS"

REM Ensure claude CLI and node are on PATH for CC subprocesses
set "PATH=C:\Users\exast\.local\bin;C:\Program Files\nodejs;C:\Users\exast\AppData\Local\Programs\Ollama;%PATH%"

:loop
python -u server.py
echo Server exited. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
