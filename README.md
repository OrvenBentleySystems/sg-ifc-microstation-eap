# IFC+SG Checker for MicroStation

An IFC+SG pre-flight checker for CORENET X submissions, for MicroStation
CONNECT Edition, MicroStation 2023 to 2026, and PowerPlatform products such as
OpenBuildings Designer.

This is an unofficial plugin for MicroStation created by Orven Fajardo. It is
not an official Bentley Systems plugin, product, or support offering. Bentley
Systems does not maintain or endorse this repository.

The checker reads an IFC file opened or referenced in MicroStation, evaluates
it against the selected CORENET X Code of Practice (COP) edition, displays
findings, selects matching model elements, and exports text, CSV, JSON, HTML,
and BCF reports.

This is not the official CORENET X Model Checker and is not a compliance
determination. The Qualified Person remains responsible for the submission.

## Requirements

| Release | How the checker runs |
| --- | --- |
| MicroStation 2024, 2025, 2026 and PowerPlatform products built on them | Inside MicroStation, with selection of failing elements |
| MicroStation CONNECT Edition, 2023 and older PowerPlatform products | Standalone window from the Start menu |

- Windows, administrator rights for installation
- No third-party Python packages. The standalone window uses the Python that
  ships with MicroStation 2024 or later, or any Python 3.10+ with tcl/tk.

## Install

1. Download a release ZIP, or clone the repository.
2. Right-click `Deploy.bat` and select **Run as administrator**.
3. Restart MicroStation.

`Deploy.bat` finds every MicroStation-based product on the machine and writes
`IFCSG_Checker.cfg` to each product's `<install>\config\appl\` folder.
MicroStation includes that folder for every WorkSpace, including organisations
with a custom or managed configuration. Products whose `msconfig.cfg` does not
include `config\appl` fall back to their ProgramData `Organization` folder.

The checker itself is installed to:

```text
C:\ProgramData\Bentley\IFCSG_Checker
```

To see what would be registered without changing anything:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\tools\microstation\install_ifcsg_checker.ps1 -ListProducts
```

Re-run `Deploy.bat` after installing a new MicroStation release. Installs made
by version 1.3 or earlier are migrated automatically, including a customised
ribbon button library.

Uninstall: right-click `Uninstall.bat` and select **Run as administrator**.

### Ribbon button

The installer creates `dgnlib\IFCSG_Checker.dgnlib` in the install folder and
adds it to `MS_GUIDGNLIBLIST`. Create the button once with **File > Settings >
Configuration > Customize** and **Customize Ribbon**; the installer prints the
exact steps. Then run `share_ifcsg_checker.ps1` and the package carries the
button to every other user.

Organisations that assign `MS_GUIDGNLIBLIST` with `=` in their own
configuration must add the line from `IFCSG_Checker.cfg` themselves.

### IFC reference compatibility

Deployment enables:

```text
IFC_ALLOW_DEPRECATED_SCHEMA = 1
```

This is the Bentley configuration fix documented in
[KB0098741](https://bentleysystems.service-now.com/community?id=kb_article_view&sysparm_article=KB0098741)
for IFC files that cannot be opened, imported, or attached because they use
deprecated schema definitions. It broadens the IFC files accepted by
MicroStation. It does not repair invalid IFC data and does not make an old
schema valid for an IFC+SG submission. The checker still reports a schema
failure when the submission schema is not IFC4.

To install without this setting:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\tools\microstation\install_ifcsg_checker.ps1 `
  -DisableDeprecatedIfcSchemas
```

See [IFC reference compatibility](docs/IFC_REFERENCE_COMPATIBILITY.md) for
version evidence, limitations, and rollback.

## Run

MicroStation 2024 or later:

```text
python load $(IFCSG_CHECKER_LAUNCHER)
```

Any release, or without MicroStation: **Start menu > IFC+SG Checker**.

The source list includes the active IFC, IFC references, DGN models, and IFC
files selected with **Browse IFC**. Click **Run check**. Large IFC files are
parsed on a worker thread so the MicroStation interface remains responsive.
**Cancel** stops parsing or rule evaluation at the next checkpoint. Click a
status chip (FAIL, WARN, UNKNOWN, PASS) to toggle that status in the list.

## COP selector

The **Code of Practice** drop-down at the top right selects the edition the
model is checked against. The newest installed edition is the default and the
last choice is remembered. Changing the edition re-checks the already parsed
model without reading the IFC again.

An older edition is labelled superseded in the window, in rule `DOC.001`, and
in every report. Use it only for projects still assessed under that edition.

Headless:

```powershell
python tools\microstation\run_ifcsg_checker.py --list-cops
python tools\microstation\run_ifcsg_checker.py --ifc model.ifc --cop 3.1 --out report.txt
```

## Catalogues

Each COP edition is one JSON file:

```text
data\catalogues\cop-4.json
data\catalogues\cop-3.1.json
```

Schema: `schema\ifcsg.catalogue.schema.json`

| COP edition | Published | IFC+SG mapping | Property sets | Properties | Identified components |
| --- | --- | --- | ---: | ---: | ---: |
| 4 | 2026-09 | 2026-09-25 (`industry-mapping-09-2026.xlsx`) | 172 | 1,762 | 131 |
| 3.1 | 2025-12 | 2025-12-04 (`industry-mapping-4-dec-2025.xlsx`) | 163 | 1,620 | 97 |

COP 4 was built from COP 3.1 plus the September 2026 BCA workbook. The 13
property definitions the new workbook no longer publishes were removed and are
listed in the catalogue metadata (`removed_properties`). Examples:
`SGPset_Door.SelfClosing` moved to `Pset_DoorCommon`, `SGPset_Wall.IsExternal`
moved to `Pset_WallCommon`, and `DoubleBayFaçade` became `DoubleBayFacade`.
Source file names and SHA-256 hashes are recorded in each catalogue.

### Adding the next COP edition

Download the new official workbook, then:

```powershell
python tools\build_catalogue.py "industry-mapping-NEW.xlsx" `
  --cop-edition 5 --from 4 `
  --mapping-edition YYYY-MM-DD `
  --previous-mapping "industry-mapping-PREVIOUS.xlsx" `
  --cop-published YYYY-MM `
  --cop-title "CORENET X Code of Practice 5th Edition" `
  --cop-url "https://info.corenet.gov.sg/..."
python tools\validate_catalogue.py
```

`--from` seeds the new edition from the previous one, `--previous-mapping`
removes properties that BCA withdrew or renamed, and the older edition stays
selectable. To refresh an existing edition, omit `--from`. Updates are atomic.

## Large model behavior

The parser uses lean mode by default. It counts all IFC records but does not
retain large face, loop, and point records that are not needed by semantic
checks.

On the included production test case:

- IFC size: 194.6 MB
- IFC records: 3,689,177
- Peak checker memory: about 106 MB
- Check time: about 13 seconds on the development machine

Coincident geometry analysis is marked not applicable in lean mode. Selection
uses IFC `GlobalId` identity where MicroStation exposes it. The removed
geometry fallback and zoom function are not used.

## Reports

The checker exports:

- Text
- CSV
- JSON
- HTML
- BCF 2.1

Large UI result sets and BCF selections are capped for usability. Exported
reports retain the complete findings.

## Tests

Run from the repository root:

```powershell
python tools\microstation\tests\featuretest.py
python tools\microstation\tests\stabilitytest.py
python tools\microstation\tests\cataloguetest.py
python tools\microstation\tests\coptest.py
python tools\microstation\tests\sourcetest.py
python tools\microstation\tests\locatortest.py
python tools\microstation\uicheck.py
python tools\validate_catalogue.py
powershell -ExecutionPolicy Bypass -File tools\microstation\tests\installtest.ps1
```

Test a real IFC and all report formats:

```powershell
python tools\microstation\tests\realmodeltest.py `
  --ifc "model.ifc" `
  --exports `
  --max-seconds 60 `
  --max-peak-mb 500
```

## Repository layout

```text
data\catalogues\cop-*.json
schema\ifcsg.catalogue.schema.json
tools\build_catalogue.py
tools\validate_catalogue.py
tools\microstation\run_ifcsg_checker.py
tools\microstation\install_ifcsg_checker.ps1
tools\microstation\share_ifcsg_checker.ps1
tools\microstation\ifcsg_checker\
tools\microstation\tests\
Deploy.bat
Uninstall.bat
```

`share_ifcsg_checker.ps1` creates a versioned distribution folder or ZIP from
an installed copy or the repository (`-Source`). Tagged releases build the same
ZIP automatically.

## Scope

The checker reads and audits IFC data. It does not author IFC+SG content or
replace OpenBuildings Designer export configuration.

Rules that cannot be evaluated return `UNKNOWN` or `NOT_APPLICABLE`. They are
never reported as passing.

## Official references

- [CORENET X Code of Practice](https://info.corenet.gov.sg/regulatory-process/corenet-x-code-of-practice)
- [COP 4th Edition (PDF)](https://info.corenet.gov.sg/docs/default-source/default-document-library/corenet-x-cop---4-edition-2026.pdf)
- [COP 4th Edition summary of changes (PDF)](https://info.corenet.gov.sg/docs/default-source/default-document-library/annex---summary-of-changes-cop-edition-4.pdf)
- [IFC+SG Excel Mapping File](https://info.corenet.gov.sg/ifc-sg/requirements---submission/ifc-sg-excel-mapping-file)
- [IFC+SG Onboarding Checklist](https://info.corenet.gov.sg/ifc-sg/start-here/ifcsg-onboarding-checklist)
- [CORENET X Model Checker](https://info.corenet.gov.sg/model-checker)
- [OpenBuildings IFC+SG resources](https://info.corenet.gov.sg/ifc-sg/modelling---authoring/bim-authoring-tools/openbuildings)

## License

Source code is MIT licensed. See `LICENSE`. Catalogue source attribution is in
`NOTICE` and the metadata of each `data\catalogues\cop-*.json`.
