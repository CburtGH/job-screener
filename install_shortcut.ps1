# Sets this checkout up as one JobScout environment and creates its Desktop + Start menu shortcut.
#   production (default):  powershell -ExecutionPolicy Bypass -File .\install_shortcut.ps1
#   staging:                powershell -ExecutionPolicy Bypass -File .\install_shortcut.ps1 -Environment staging
param([ValidateSet("production", "staging")][string]$Environment = "production")
Set-Location $PSScriptRoot

# instance.json tells the app which environment this folder is (git-ignored, so each checkout keeps its own).
if ($Environment -eq "staging") {
    $prod = Join-Path (Split-Path $PSScriptRoot -Parent) "job-screener"
    @{ env = "staging"; production_path = $prod } | ConvertTo-Json | Set-Content -Encoding ascii instance.json
    $name = "JobScout (Staging)"; $icon = "icon-staging.ico"
} else {
    if (Test-Path instance.json) { Remove-Item instance.json -Confirm:$false }
    $name = "JobScout"; $icon = "icon.ico"
}

# Use the repo's git hooks (pre-commit checks AGENTS.md is current). Shared by all checkouts of this repo.
if (Get-Command git -ErrorAction SilentlyContinue) { git config core.hooksPath .githooks }

if (-not (Test-Path .venv)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
}
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if (-not (Test-Path "static\$icon")) { .\.venv\Scripts\python.exe tools\make_icon.py }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $lnk = $shell.CreateShortcut((Join-Path $dir "$name.lnk"))
    $lnk.TargetPath = (Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe")
    $lnk.Arguments = "-m app"
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.IconLocation = (Join-Path $PSScriptRoot "static\$icon")
    $lnk.Description = "$name job screener"
    $lnk.Save()
    Write-Host "Created $(Join-Path $dir "$name.lnk")"
}
