# Starts the Payslip Sender app. Usage (in the project folder):  .\run.ps1
# Uses the project's own Python, so you don't need to activate the virtual environment first.
# (We run "python -m uvicorn" because Windows blocks the plain uvicorn.exe launcher.)
Set-Location -Path $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload
