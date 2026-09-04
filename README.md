# IFC+SG Checker for MicroStation

An IFC+SG pre-flight checker for MicroStation 2026 and compatible
OpenBuildings Designer releases.

This is an unofficial plugin for MicroStation created by Orven Fajardo. It is
not an official Bentley Systems plugin, product, or support offering. Bentley
Systems does not maintain or endorse this repository.

The checker reads an IFC file opened or referenced in MicroStation, evaluates
local validation rules, displays findings, selects matching model elements, and
exports text, CSV, JSON, HTML, and BCF reports.

This is not the official CORENET X Model Checker and is not a compliance
determination. The Qualified Person remains responsible for the submission.

## Requirements

- Windows
- MicroStation 2026 or a compatible OpenBuildings Designer release
- MicroStation Python

No separate Python installation or third-party Python package is required for
normal use.

## Install

Download or clone the repository. Right-click `Deploy.bat` and select
**Run as administrator**.

The default install folder is:

```text
C:\ProgramData\Bentley\IFCSG_Checker
```

Restart MicroStation after installation.

Uninstall:

```powershell
powershell -ExecutionPolicy Bypass -File `
  .\tools\microstation\install_ifcsg_checker.ps1 -Uninstall
```

## Run

Use this MicroStation key-in:

```text
python load $(IFCSG_CHECKER_LAUNCHER)
```

The source list includes the active IFC, IFC references, DGN models, and IFC
files selected with **Browse IFC**.

Click **Run check**. Large IFC files are parsed on a worker thread so the
MicroStation interface remains responsive. **Cancel** stops parsing or rule
evaluation at the next checkpoint.

## Catalogue

Runtime data is stored in one explicit JSON file:

```text
data\catalogue.json
```

Its schema is:

```text
schema\ifcsg.catalogue.schema.json
```

The bundled catalogue is aligned to:

- CORENET X Code of Practice 3.1, December 2025
- IFC+SG Excel Mapping File, 4 December 2025
- Official workbook SHA-256:
  `f436aa273c366f04dcf6eef55e365081a8b76a2271520c188174a1c69f803114`
- Verified against the official CORENET X pages on 4 September 2026

The catalogue contains 163 property sets, 1,620 unique property definitions,
97 identified components, entity domains, area schemes, and 28 local rules.

To update the catalogue, download the current official workbook and run:

```powershell
python tools\build_catalogue.py "industry-mapping.xlsx" `
  --mapping-edition YYYY-MM-DD `
  --cop-edition VERSION
```

The update is atomic. Source file names and hashes are recorded in the
catalogue metadata.

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
python tools\microstation\tests\sourcetest.py
python tools\microstation\tests\locatortest.py
python tools\microstation\uicheck.py
python tools\validate_catalogue.py
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
data\catalogue.json
schema\ifcsg.catalogue.schema.json
tools\build_catalogue.py
tools\microstation\run_ifcsg_checker.py
tools\microstation\ifcsg_checker\
tools\microstation\tests\
Deploy.bat
```

`share_ifcsg_checker.ps1` creates a dated distribution folder or ZIP from an
installed copy.

## Scope

The checker reads and audits IFC data. It does not author IFC+SG content or
replace OpenBuildings Designer export configuration.

Rules that cannot be evaluated return `UNKNOWN` or `NOT_APPLICABLE`. They are
never reported as passing.

## Official references

- [CORENET X Code of Practice](https://info.corenet.gov.sg/regulatory-process/corenet-x-code-of-practice)
- [IFC+SG Excel Mapping File](https://info.corenet.gov.sg/ifc-sg/requirements---submission/ifc-sg-excel-mapping-file)
- [IFC+SG Onboarding Checklist](https://info.corenet.gov.sg/ifc-sg/start-here/ifcsg-onboarding-checklist)
- [CORENET X Model Checker](https://info.corenet.gov.sg/model-checker)
- [OpenBuildings IFC+SG resources](https://info.corenet.gov.sg/ifc-sg/modelling---authoring/bim-authoring-tools/openbuildings)

## License

Source code is MIT licensed. See `LICENSE`. Catalogue source attribution is in
`NOTICE` and `data\catalogue.json`.
