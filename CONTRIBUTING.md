# Contributing

## Changes

- Keep the checker compatible with Python 3.10 and later.
- Do not add runtime dependencies without a clear need.
- Keep MicroStation API calls on the main thread.
- Keep parsing and rule evaluation independent of MicroStation.
- Do not add an IFC+SG property, value, or rule from memory.

## Catalogue updates

Use the current official BCA mapping workbook. For a new COP edition:

```powershell
python tools\build_catalogue.py "industry-mapping-NEW.xlsx" `
  --cop-edition VERSION --from PREVIOUS_VERSION `
  --mapping-edition YYYY-MM-DD `
  --previous-mapping "industry-mapping-PREVIOUS.xlsx" `
  --cop-published YYYY-MM
```

To refresh an existing edition, omit `--from` and `--previous-mapping`.

Review the changed source hashes, property counts, datatype changes and
`removed_properties` before committing `data/catalogues/`. Then reconcile the
edition with its COP PDF (`pip install pypdf` first; build-time only) and
validate:

```powershell
python tools\reconcile_cop_pdf.py "cop.pdf" --cop VERSION --apply
python tools\validate_catalogue.py
```

## Tests

Run:

```powershell
python tools\microstation\tests\featuretest.py
python tools\microstation\tests\stabilitytest.py
python tools\microstation\tests\cataloguetest.py
python tools\microstation\tests\coptest.py
python tools\microstation\tests\sourcetest.py
python tools\microstation\tests\locatortest.py
python tools\microstation\uicheck.py
```

For parser or rule changes, also run `realmodeltest.py` against a representative
production IFC.
