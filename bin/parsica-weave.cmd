@echo off
rem Parsica Weave CLI (Windows shim): pick a WORKING Python 3 - probe each candidate and
rem skip any that fails (the Microsoft Store `python3` stub exits nonzero on the probe),
rem then hand off to the python dispatcher beside us.
setlocal
set "PROBE=import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)"
python3 -c "%PROBE%" >nul 2>nul && goto :run_python3
python -c "%PROBE%" >nul 2>nul && goto :run_python
py -3 -c "%PROBE%" >nul 2>nul && goto :run_py
echo parsica-weave: no working Python 3 found ^(tried python3, python, py -3^). 1>&2
exit /b 1
:run_python3
python3 "%~dp0parsica-weave.py" %*
exit /b %errorlevel%
:run_python
python "%~dp0parsica-weave.py" %*
exit /b %errorlevel%
:run_py
py -3 "%~dp0parsica-weave.py" %*
exit /b %errorlevel%
