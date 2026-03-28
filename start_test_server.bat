@echo off
REM Launch Loom test server on port 3001 with a separate database.
REM Shutdown: curl -X POST http://localhost:3001/shutdown
REM   or:     python stop_test_server.py

set LOOM_PORT=3001
set LOOM_DB=loom_test.db

echo ============================================
echo   Loom TEST server starting on port %LOOM_PORT%
echo   DB: %LOOM_DB%
echo   Shutdown: curl -X POST http://localhost:%LOOM_PORT%/shutdown
echo ============================================

python server.py
