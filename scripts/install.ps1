# tokengate one-line installer for Windows (PowerShell). Run inside your project folder:
#   irm https://raw.githubusercontent.com/dgrudlev-sys/tokengate/main/scripts/install.ps1 | iex
$ErrorActionPreference = "Stop"
# (works on Windows PowerShell 5.1 and PowerShell 7)
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) { Write-Host "Python 3.10+ is required: winget install Python.Python.3.12"; exit 1 }
& $py.Source -m pip install --upgrade "tokengate[mcp] @ git+https://github.com/dgrudlev-sys/tokengate.git"
& $py.Source -m tokengate.cli init .
& $py.Source -m tokengate.cli doctor --offline
