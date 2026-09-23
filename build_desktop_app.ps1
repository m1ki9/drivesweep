$ErrorActionPreference = "Stop"

python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin --name DriveSweep `
    --icon assets/icon.ico --add-data "cleaner/ui;cleaner/ui" run.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Built dist\DriveSweep.exe - double-click it once and it adds Desktop and Start menu shortcuts."
