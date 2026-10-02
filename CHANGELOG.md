# Changelog

## 1.4.0

- Added a COP selector. The window, headless runner (`--cop`, `--list-cops`)
  and every report use the selected CORENET X Code of Practice edition.
  Changing edition re-checks the parsed model without re-reading the IFC.
- Added the COP 4th Edition (September 2026) catalogue, built from the BCA
  `industry-mapping-09-2026.xlsx` workbook: 172 property sets, 1,762
  properties, 131 identified components. Kept COP 3.1 selectable.
- `DOC.001` and reports flag a superseded COP edition.
- Moved catalogues to `data/catalogues/cop-<edition>.json`.
- `build_catalogue.py` can seed a new edition (`--from`) and remove properties
  withdrawn by BCA (`--previous-mapping`). Workbook enum sheets such as
  Industrial Activity Type are imported. Workbook enumerations are authoritative.
- Redesigned the checker window: header with COP selector, source card,
  status chips, flat theme, clearer rule and finding rows.
- The installer now registers with every detected MicroStation-based product
  through `<install>\config\appl`, which also works with custom and managed
  configurations, and migrates 1.3.x installs including a customised ribbon
  library.
- Added a standalone launcher and Start menu shortcut for releases without
  MicroStation Python (CONNECT Edition, 2023).
- Added `Uninstall.bat` and `-ListProducts`.
- Removed the unused `install_ribbon_dgnlib.ps1`.

## 1.3.2

- Added the `IFC_ALLOW_DEPRECATED_SCHEMA=1` deployment setting from Bentley
  KB0098741.
- Added an installer switch to disable the compatibility setting.
- Added repeat-install and uninstall coverage for the setting.

## 1.3.1

- Added explicit independent-project and non-endorsement notices.

## 1.3.0

- Consolidated runtime data into `data/catalogue.json`.
- Rebuilt catalogue metadata from the official 4 December 2025 BCA workbook.
- Fixed official workbook column detection and duplicate row handling.
- Added controlled values and identified components from the workbook.
- Added lean IFC parsing for large files.
- Reduced the production test peak memory from about 840 MB to about 106 MB.
- Removed the unsupported zoom action.
- Verified and tested Clear filters.
- Removed in-app catalogue mutation.
- Added repository deployment, license, ignore rules, and GitHub Actions tests.
- Removed unrelated STAAD and MCP experiment content.
