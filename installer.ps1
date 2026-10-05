$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Impossible de créer le virtualenv. Installe Python 3.11 ou supérieur.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Installation des dépendances échouée.' }
& '.\.venv\Scripts\python.exe' -m playwright install chromium
if ($LASTEXITCODE -ne 0) { throw 'Installation du navigateur échouée.' }
Write-Host 'Installation terminée. Lance configurer.cmd, connexion.cmd, puis demarrer.cmd.'
