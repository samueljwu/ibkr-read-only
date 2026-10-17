$ErrorActionPreference = 'Stop'
$version = (& python -c 'from app.version import VERSION; print(VERSION)').Trim()
if ($LASTEXITCODE -ne 0 -or !$version) { throw 'Could not read the application version.' }
$bundle = Join-Path (Get-Location) 'dist/PortfolioMonitor/PortfolioMonitor.exe'
if (!(Test-Path -LiteralPath $bundle)) { throw 'Build the PyInstaller bundle first.' }

$compiler = if ($env:ISCC_PATH) { $env:ISCC_PATH } else {
    $found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($found) { $found.Source } else {
        @(
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
            'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}
if (!$compiler -or !(Test-Path -LiteralPath $compiler)) { throw 'Inno Setup 6 ISCC.exe was not found. Set ISCC_PATH to its full path.' }

$arguments = @("/DAppVersion=$version", 'packaging/windows/PortfolioMonitor.iss')
& $compiler @arguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE." }

$installer = "dist/installers/PortfolioMonitor-$version-windows-x64-setup.exe"
if (!(Test-Path -LiteralPath $installer)) { throw "Installer was not produced: $installer" }
Write-Output (Resolve-Path -LiteralPath $installer)
