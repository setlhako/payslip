# Developer shortcuts (like a "make" file). Usage, in the project folder:   .\dev.ps1 <task>
#   .\dev.ps1 install   install the exact pinned packages (requirements-dev.txt)
#   .\dev.ps1 test      run all the tests (about 3 minutes)
#   .\dev.ps1 audit     check the pinned packages for known security problems (needs internet)
#   .\dev.ps1 check     the production readiness check (reads your .env; on your own PC it says FAIL for APP_ENV: that is expected)
#   .\dev.ps1 all       install, test and audit
# Uses the project's own Python, so you don't need to activate the virtual environment first.
param([Parameter(Position = 0)][string]$Task = "help")

Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "No .venv found. Create it first: python -m venv .venv"; exit 1 }

function Run-Step([string]$Title, [string[]]$Arguments) {
    Write-Host "`n== $Title" -ForegroundColor Cyan
    & $py @Arguments
    return $LASTEXITCODE
}

switch ($Task) {
    "install" { exit (Run-Step "Installing pinned packages" @("-m", "pip", "install", "-r", "requirements-dev.txt")) }
    "test"    { exit (Run-Step "Running the tests" @("-m", "pytest", "-q")) }
    "audit"   {
        # --disable-pip: look the pinned versions up directly; -r: audit the files, not whatever happens to be installed.
        $a = Run-Step "Auditing requirements.txt" @("-W", "ignore", "-m", "pip_audit", "-r", "requirements.txt", "--disable-pip", "--no-deps")
        $b = Run-Step "Auditing requirements-dev.txt" @("-W", "ignore", "-m", "pip_audit", "-r", "requirements-dev.txt", "--disable-pip", "--no-deps")
        if ($a -ne 0 -or $b -ne 0) { Write-Host "`nVulnerabilities found: see above. Raise the pinned version in requirements.txt, then run .\dev.ps1 test." -ForegroundColor Yellow; exit 1 }
        Write-Host "`nNo known vulnerabilities." -ForegroundColor Green; exit 0
    }
    "check"   { exit (Run-Step "Production readiness check" @("-m", "app.scripts.production_check")) }
    "all"     {
        $i = Run-Step "Installing pinned packages" @("-m", "pip", "install", "-r", "requirements-dev.txt")
        if ($i -ne 0) { exit $i }
        $t = Run-Step "Running the tests" @("-m", "pytest", "-q")
        $a = Run-Step "Auditing requirements.txt" @("-W", "ignore", "-m", "pip_audit", "-r", "requirements.txt", "--disable-pip", "--no-deps")
        if ($t -ne 0 -or $a -ne 0) { exit 1 }
        Write-Host "`nAll good." -ForegroundColor Green; exit 0
    }
    default   { Get-Content $PSCommandPath -TotalCount 8 | ForEach-Object { $_ -replace '^# ?', '' }; exit 0 }
}
