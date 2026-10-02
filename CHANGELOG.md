# Changelog

## 1.5.0

- Added the **Objects to fix** tab: one row per object, errors first, with a
  side panel that shows the object's picture, storey, GlobalId and every
  reason it fails with the fix. Opens on the first object after a check;
  arrow keys step through.
- Added object pictures: drawn from the referenced MicroStation element by
  GlobalId, or from IFC geometry (files up to 60 MB) without MicroStation.
- The HTML report has an **Objects to fix** section with a picture card for
  each failed object (first 150) and a table for the rest. The command-line
  report includes pictures when the IFC geometry is available.
- Reconciled COP 3.1 with its official PDF and added subtype reconciliation
  for both editions (`RINSESHOWER` added to COP 4).
- Fixed: workbook rows whose property set is "N.A" dropped their subtype
  (for example `*DROPINLETCHAMBER`), so correctly classified objects were
  reported as unmapped. COP 3.1 regains 14 identified components.
- Verified live in MicroStation 2026 on an IFC2x3 reference: GlobalId
  selection, pictures for every failing class, and the report.

## 1.4.1

- Fixed: required property sets were the union of every identified component
  for an entity (for example 18 sets on every IfcSpace, including withdrawn
  ones). They now come from the element's identified component via its
  subtype token; only the component's main SGPset is an ERROR when missing.
- Fixed: the workbook's "IFC Sub Types" column was never read, so components
  had no subtypes. Components now store subtype-to-property-set variants.
- Fixed: subtype rules read IFC4 attribute positions on IFC2X3 files and
  reported wrong values; they are now not applicable to non-IFC4 files.
- Fixed: every IfcBuildingElementProxy was an error, including mapped COP
  components and curtain-wall parts.
- Fixed: CLASS.003 warned on every USERDEFINED element without checking it.
- Fixed: property-set findings on the project, site, building or storey showed
  no object.
- Fixed: a property moved to another set was reported as a near miss of an
  unrelated name.
- Fixed: MicroStation selection ignored the GlobalId of non-IFC4 references.
- Added `tools/reconcile_cop_pdf.py` and reconciled COP 4 with the COP 4 PDF:
  6 properties added, 10 datatype differences accepted.
- The window shows GlobalId and storey for the selected finding, labels
  file-level rules, and adds Copy GlobalId.

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
