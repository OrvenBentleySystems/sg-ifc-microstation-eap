$ErrorActionPreference = "Stop"

$root = Split-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) -Parent
$installer = Join-Path $root "tools\microstation\install_ifcsg_checker.ps1"
$temp = Join-Path ([IO.Path]::GetTempPath()) ("ifcsg-install-" + [guid]::NewGuid())
$target = Join-Path $temp "install"
$programData = Join-Path $temp "ProgramData\Bentley"

function New-FakeProduct($name, [switch]$Python) {
    $msdir = Join-Path $temp "Program Files\Bentley\$name\MicroStation"
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $msdir "config\appl")
    Set-Content (Join-Path $msdir "config\msconfig.cfg") @(
        "%level Application",
        "%include `$(_USTN_APPL)*.cfg") -Encoding ASCII
    $null = New-Item -ItemType Directory -Force -Path (Join-Path $msdir "Default\Data")
    Set-Content (Join-Path $msdir "Default\Data\PersonalDgnlibSeed.dgnlib") "seed" -Encoding ASCII
    if ($Python) { Set-Content (Join-Path $msdir "MSPyBentley.pyd") "" -Encoding ASCII }
    return $msdir
}

function Assert($condition, $message) {
    if (-not $condition) { throw $message }
}

$new = New-FakeProduct "MicroStation 2026" -Python
$old = New-FakeProduct "MicroStation CONNECT Edition"
$products = @($new, $old)
$common = @{ Target = $target; ProductRoot = $products; NoShortcut = $true;
             ProgramDataRoot = $programData }

# A 1.3.x install registered in the Organization folder with a customised button.
$legacyOrg = Join-Path $programData "MicroStation 2026\Configuration\Organization"
$null = New-Item -ItemType Directory -Force -Path (Join-Path $legacyOrg "Dgnlib\Gui")
Set-Content (Join-Path $legacyOrg "IFCSG_Checker.cfg") "IFCSG_CHECKER_DIR = x" -Encoding ASCII
Set-Content (Join-Path $legacyOrg "Dgnlib\Gui\IFCSG_Checker.dgnlib") "legacy-button" -Encoding ASCII
Set-Content (Join-Path $legacyOrg "Other.cfg") "UNRELATED = 1" -Encoding ASCII

# A product outside -ProductRoot, registered through its Organization folder.
$otherOrg = Join-Path $programData "MicroStation 2025\Configuration\Organization"
$null = New-Item -ItemType Directory -Force -Path $otherOrg
$otherCfg = Join-Path $otherOrg "IFCSG_Checker.cfg"
Set-Content $otherCfg "IFCSG_CHECKER_DIR       = $target\" -Encoding ASCII

try {
    $listing = & $installer @common -ListProducts 6>&1 | Out-String
    Assert ($listing -match "MicroStation 2026") "ListProducts missed the 2026 product."
    Assert ($listing -match "MicroStation CONNECT Edition") "ListProducts missed the CONNECT product."
    Assert (-not (Test-Path $target)) "ListProducts changed the machine."

    & $installer @common | Out-Null

    foreach ($msdir in $products) {
        $cfg = Join-Path $msdir "config\appl\IFCSG_Checker.cfg"
        Assert (Test-Path $cfg) "Configuration missing for $msdir."
        $text = Get-Content $cfg -Raw
        Assert ($text -match '(?m)^\s*IFC_ALLOW_DEPRECATED_SCHEMA\s*=\s*1\s*$') `
            "Deprecated IFC compatibility setting is missing."
        Assert ($text -match 'MS_GUIDGNLIBLIST\s*>') "Ribbon library is not registered."
        Assert ($text -match [regex]::Escape("IFCSG_CHECKER_DIR       = $target\")) `
            "Install folder is not registered."
    }
    foreach ($file in @("data\catalogues\cop-4.json", "data\catalogues\cop-3.1.json",
                        "docs\IFC_REFERENCE_COMPATIBILITY.md", "IFCSG_Checker.cmd",
                        "Uninstall.bat", "icons\IFCSG_Checker.ico")) {
        Assert (Test-Path (Join-Path $target $file)) "$file was not deployed."
    }
    Assert (-not (Test-Path (Join-Path $target "tools\microstation\tests"))) `
        "Tests were deployed."
    Assert (-not (Test-Path (Join-Path $legacyOrg "IFCSG_Checker.cfg"))) `
        "Legacy Organization configuration was not removed."
    Assert (Test-Path (Join-Path $legacyOrg "Other.cfg")) "An unrelated configuration was removed."
    Assert (Test-Path $otherCfg) "A product outside -ProductRoot lost its registration."
    Assert (-not (Test-Path (Join-Path $legacyOrg "Dgnlib\Gui\IFCSG_Checker.dgnlib"))) `
        "Legacy ribbon library was left in place."
    $dgnlib = Join-Path $target "dgnlib\IFCSG_Checker.dgnlib"
    Assert ((Get-Content $dgnlib -Raw).Trim() -eq "legacy-button") `
        "Customised ribbon library was not migrated."

    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($python) {
        $cops = & $python (Join-Path $target "tools\microstation\run_ifcsg_checker.py") --list-cops |
            Out-String
        Assert ($cops -match '(?m)^4\s' -and $cops -match '(?m)^3\.1\s') `
            "Deployed checker does not list both COP editions: $cops"
    }

    & $installer @common | Out-Null
    Assert ((Get-Content $dgnlib -Raw).Trim() -eq "legacy-button") `
        "Reinstall replaced the customised ribbon library."

    & $installer @common -DisableDeprecatedIfcSchemas | Out-Null
    $text = Get-Content (Join-Path $new "config\appl\IFCSG_Checker.cfg") -Raw
    Assert ($text -notmatch '(?m)^\s*IFC_ALLOW_DEPRECATED_SCHEMA\s*=') `
        "DisableDeprecatedIfcSchemas did not remove the setting."

    & $installer @common | Out-Null
    $text = Get-Content (Join-Path $new "config\appl\IFCSG_Checker.cfg") -Raw
    Assert ($text -match '(?m)^\s*IFC_ALLOW_DEPRECATED_SCHEMA\s*=\s*1\s*$') `
        "Default reinstall did not restore the setting."

    & $installer @common -Uninstall | Out-Null
    foreach ($msdir in $products) {
        Assert (-not (Test-Path (Join-Path $msdir "config\appl\IFCSG_Checker.cfg"))) `
            "Uninstall left the configuration for $msdir."
    }
    Assert (Test-Path $otherCfg) "Scoped uninstall removed another product's registration."
    Assert (Test-Path (Join-Path $target "data\catalogues")) `
        "Scoped uninstall removed a folder another product still uses."

    Remove-Item $otherCfg -Force
    & $installer @common -Uninstall | Out-Null
    Assert (-not (Test-Path $target)) "Uninstall left the install folder."
    Write-Output "INSTALL TEST PASSED"
} finally {
    if (Test-Path $temp) {
        Remove-Item $temp -Recurse -Force
    }
}
