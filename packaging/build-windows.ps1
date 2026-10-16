$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
function Invoke-Native {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Native step failed: $Program (exit $LASTEXITCODE)" }
}
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Build on native Windows x64, not ARM64 or x86.' }
Invoke-Native -Program 'uv' -Arguments @('sync', '--locked', '--extra', 'dev')
Invoke-Native -Program '.venv\Scripts\python.exe' -Arguments @('tools/delivery.py', 'version')
Invoke-Native -Program '.venv\Scripts\python.exe' -Arguments @('tools/build_paths.py')
Invoke-Native -Program '.venv\Scripts\pyinstaller.exe' -Arguments @('--clean', '--noconfirm', '--distpath', 'dist/native', '--workpath', 'build/pyinstaller', 'packaging/VLCMonitor.spec')
Invoke-Native -Program '.venv\Scripts\python.exe' -Arguments @('tools/package_native.py')
Write-Output 'Internal unsigned build. Run Windows 10/11 device acceptance separately.'
