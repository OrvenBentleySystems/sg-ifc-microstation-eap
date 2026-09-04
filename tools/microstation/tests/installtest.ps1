$ErrorActionPreference = "Stop"

$root = Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent
$installer = Join-Path $root "tools\microstation\install_ifcsg_checker.ps1"
$temp = Join-Path ([IO.Path]::GetTempPath()) ("ifcsg-install-" + [guid]::NewGuid())
$target = Join-Path $temp "install"
$config = Join-Path $temp "MicroStation\Configuration"
$organization = Join-Path $config "Organization"
$null = New-Item -ItemType Directory -Force -Path $organization

try {
    & $installer -Target $target -MicroStationConfig $config | Out-Null

    $cfg = Join-Path $organization "IFCSG_Checker.cfg"
    if (-not (Test-Path $cfg)) { throw "Configuration file was not created." }
    $text = Get-Content $cfg -Raw
    if ($text -notmatch '(?m)^\s*IFC_ALLOW_DEPRECATED_SCHEMA\s*=\s*1\s*$') {
        throw "Deprecated IFC compatibility setting is missing."
    }
    if (-not (Test-Path (Join-Path $target "data\catalogue.json"))) {
        throw "Catalogue was not deployed."
    }
    if (-not (Test-Path (Join-Path $target "docs\IFC_REFERENCE_COMPATIBILITY.md"))) {
        throw "IFC compatibility documentation was not deployed."
    }

    & $installer -Target $target -MicroStationConfig $config | Out-Null

    & $installer -Target $target -MicroStationConfig $config `
        -DisableDeprecatedIfcSchemas | Out-Null
    $text = Get-Content $cfg -Raw
    if ($text -match '(?m)^\s*IFC_ALLOW_DEPRECATED_SCHEMA\s*=') {
        throw "DisableDeprecatedIfcSchemas did not remove the setting."
    }

    & $installer -Target $target -MicroStationConfig $config | Out-Null
    $text = Get-Content $cfg -Raw
    if ($text -notmatch '(?m)^\s*IFC_ALLOW_DEPRECATED_SCHEMA\s*=\s*1\s*$') {
        throw "Default reinstall did not restore the setting."
    }

    & $installer -Target $target -MicroStationConfig $config -Uninstall | Out-Null
    if (Test-Path $cfg) { throw "Uninstall left the configuration file." }
    if (Test-Path $target) { throw "Uninstall left the install folder." }
    Write-Output "INSTALL TEST PASSED"
} finally {
    if (Test-Path $temp) {
        Remove-Item $temp -Recurse -Force
    }
}
