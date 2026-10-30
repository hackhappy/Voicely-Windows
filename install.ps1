# Installs (or updates) Voicely for Windows: Python + app + models in ~/.voicely,
# `voicely` on PATH via ~/.local/bin, and a Start Menu shortcut. Transcripts stay in Documents\Voicely.
$ErrorActionPreference = 'Stop'
$root = Join-Path $HOME '.voicely'
$scripts = Join-Path $root 'env\Scripts'
$bin = Join-Path $HOME '.local\bin'

# A running copy locks its files.
Get-Process | Where-Object { $_.Path -like "$root\*" } | Stop-Process -Force

$env:UV_PYTHON_INSTALL_DIR = Join-Path $root 'python'
uv venv (Join-Path $root 'env') --python 3.12 --managed-python --allow-existing
if ($LASTEXITCODE) { throw 'uv venv failed' }
uv pip install --python (Join-Path $scripts 'python.exe') --link-mode copy --reinstall-package voicely $PSScriptRoot
if ($LASTEXITCODE) { throw 'uv pip install failed' }

New-Item -ItemType Directory -Force $bin | Out-Null
Copy-Item (Join-Path $scripts 'voicely.exe') $bin -Force

& (Join-Path $scripts 'voicely.exe') download
if ($LASTEXITCODE) { throw 'model download failed' }

$ico = Join-Path $root 'voicely.ico'
& (Join-Path $scripts 'python.exe') -c "import sys, pathlib, voicely; from PIL import Image; Image.open(pathlib.Path(voicely.__file__).with_name('icon.png')).save(sys.argv[1], sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])" $ico
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Voicely.lnk'))
$shortcut.TargetPath = Join-Path $scripts 'voicely-app.exe'
$shortcut.IconLocation = $ico
$shortcut.Description = 'Offline dictation and transcription'
$shortcut.Save()

if (($env:Path -split ';') -notcontains $bin) { Write-Warning "$bin is not on PATH; add it to use the voicely command." }
Write-Host 'Voicely installed. Start it from the Start Menu (Voicely); dictate with Alt+Space.'
