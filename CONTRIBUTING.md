# Contributing

## Changes

- Keep the checker compatible with Python 3.10 and later.
- Do not add runtime dependencies without a clear need.
- Keep MicroStation API calls on the main thread.
- Keep parsing and rule evaluation independent of MicroStation.
- Do not add an IFC+SG property, value, or rule from memory.

## Catalogue updates

Use the current official BCA mapping workbook:

```powershell
python tools\build_catalogue.py "industry-mapping.xlsx" `
  --mapping-edition YYYY-MM-DD `
  --cop-edition VERSION
```

Review the changed source hashes, property counts, and datatype changes before
committing `data/catalogue.json`.

## Tests

Run:

```powershell
python tools\microstation\tests\featuretest.py
python tools\microstation\tests\stabilitytest.py
python tools\microstation\tests\cataloguetest.py
python tools\microstation\tests\sourcetest.py
python tools\microstation\tests\locatortest.py
python tools\microstation\uicheck.py
```

For parser or rule changes, also run `realmodeltest.py` against a representative
production IFC.
