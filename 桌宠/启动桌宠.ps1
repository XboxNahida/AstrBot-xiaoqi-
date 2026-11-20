param([ValidateSet('classic','dynamic')][string]$Mode = 'dynamic')
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先按纯净版使用说明安装依赖。' }
Start-Process -FilePath $python -ArgumentList @('-X','utf8','original_main.py','--mode',$Mode) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
