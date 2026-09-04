<#
.SYNOPSIS
    Copies the packaged ribbon button library over the one the installer creates.

.DESCRIPTION
    Called by Deploy.bat when the package ships a dgnlib carrying the ribbon
    button, so recipients never open the Customize dialog.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Package
)

$ErrorActionPreference = "Stop"

$source = Join-Path $Package "dgnlib\IFCSG_Checker.dgnlib"
if (-not (Test-Path $source)) {
    Write-Host "  no ribbon library in this package - skipping"
    exit 0
}

$gui = Get-ChildItem "C:\ProgramData\Bentley" -Recurse -Directory -Filter "Gui" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -like "*Dgnlib*" } |
    Select-Object -First 1

if (-not $gui) {
    Write-Warning "  No Dgnlib\Gui folder found. Create the button manually - see README.txt."
    exit 0
}

Copy-Item $source (Join-Path $gui.FullName "IFCSG_Checker.dgnlib") -Force
Write-Host ("  ribbon library installed to " + $gui.FullName)
