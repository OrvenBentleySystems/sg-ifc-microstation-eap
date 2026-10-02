#!/usr/bin/env python3
"""Reconcile a COP catalogue against the property tables in the official COP PDF.

The BCA mapping workbook and the COP PDF are published separately and do not
always agree. This tool parses every "IFC Entity / IFC SubType / property"
table in the PDF and reports, per entity:

    missing   a property the PDF lists that the catalogue does not have
    conflict  a property whose PDF datatype differs from the catalogue

With --apply, missing properties are added (marked with their PDF source) and
conflicting datatypes are accepted alongside the workbook datatype, so a model
that follows either official document is not failed. Nothing is removed.

Needs pypdf at build time only:  pip install pypdf

    python tools/reconcile_cop_pdf.py cop4.pdf --cop 4            # report
    python tools/reconcile_cop_pdf.py cop4.pdf --cop 4 --apply    # update
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "microstation"))

from ifcsg_checker import importer  # noqa: E402
from ifcsg_checker.library import Library  # noqa: E402

PDF_TYPES = "Text|Boolean|Length|Integer|Real|Area|Volume|Label|Count|Identifier|Mass|Time"

# IFC entity attributes the COP lists beside properties. They are exported as
# attributes, not property-set properties.
NATIVE_ATTRIBUTES = {
    "Name", "LongName", "Description", "ObjectType", "Tag", "PredefinedType",
    "OverallWidth", "OverallHeight", "OperationType", "NumberOfRisers",
    "NumberOfTreads", "RiserHeight", "TreadLength",
}

# Misspellings in the PDF where the catalogue already holds the intended name.
KNOWN_PDF_TYPOS = {"BarrierFreeAccessbility": "BarrierFreeAccessibility"}

# Values the PDF uses for a datatype that are evidently wrong for the property.
IGNORED_TYPE_CONFLICTS = {("IfcSpace", "Area", "Length")}


def pdf_text(path):
    try:
        import pypdf
    except ImportError:
        raise SystemExit("pypdf is required: pip install pypdf")
    reader = pypdf.PdfReader(path)
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_tables(text):
    """[(entity, subtype text, property, pdf datatype)] from every COP table."""
    out = []
    pattern = re.compile(
        r"IFC Entity:\s*(Ifc\w+)\s*\n\s*IFC SubType:\s*(.*?)\n\s*S/N IFC\+SG Property[^\n]*\n"
        r"(.*?)(?=IFC Entity:|Section 4:|Typical Components|\Z)", re.S)
    row = re.compile(r"(?m)^\s*\d+\s+([A-Za-z][A-Za-z0-9_]*)\^?\s+(" + PDF_TYPES + r")\b")
    for block in pattern.finditer(text):
        entity, subtype, body = block.group(1), block.group(2).strip(), block.group(3)
        for match in row.finditer(body):
            out.append((entity, subtype, match.group(1), match.group(2)))
    return out


def parse_subtypes(text):
    """{entity: [subtype tokens]} from every 'IFC Entity / IFC SubType' heading."""
    out = {}
    for entity, sub in re.findall(r"IFC Entity:\s*(Ifc\w+)\s*\n\s*IFC SubType:\s*(.*?)\n", text):
        for token in sub.split(","):
            token = re.sub(r"[^A-Z0-9_]", "", token.upper())
            if token and token not in ("NA",):
                bucket = out.setdefault(entity, [])
                if token not in bucket:
                    bucket.append(token)
    return out


def missing_subtypes(library, pdf_subtypes):
    out = {}
    for entity, tokens in sorted(pdf_subtypes.items()):
        have = library.valid_subtypes(entity)
        if entity.upper() == "IFCSPACE":
            have |= set(t.lstrip("*").upper() for t in library.area_scheme_tokens())
        extra = [t for t in tokens if t not in have]
        if extra:
            out[entity] = extra
    return out


def _entity_index(catalogue):
    index = {}
    for pset in catalogue["property_sets"]:
        for entity in pset.get("entities") or []:
            for prop in pset.get("properties") or []:
                index.setdefault(entity.lower(), {}).setdefault(prop["name"], []).append(
                    (pset, prop))
    for scheme in catalogue.get("area_schemes") or []:
        for prop in scheme.get("properties") or []:
            index.setdefault("ifcspace", {}).setdefault(prop["name"], []).append(
                (scheme, prop))
    return index


def _target_set(catalogue, entity, prop):
    """Property set the PDF row belongs to; the COP tables do not name it."""
    names = dict((p["name"], p) for p in catalogue["property_sets"])
    base = entity[3:]
    # IsExternal is a standard IFC Common property; COP 4 moved it out of SGPsets.
    if prop == "IsExternal":
        candidates = ["Pset_%sCommon" % base]
    else:
        candidates = ["SGPset_%s" % base]
    for name in candidates:
        if name in names:
            return names[name], False
    name = candidates[0]
    pset = {"name": name, "binding": "I", "entities": [entity], "subtypes": [],
            "disciplines": [], "agencies": [], "gateways": [], "verified": True,
            "source": "COP PDF", "properties": []}
    catalogue["property_sets"].append(pset)
    return pset, True


def reconcile(catalogue, rows):
    index = _entity_index(catalogue)
    missing, conflicts, typos = {}, {}, {}
    for entity, subtype, prop, pdf_type in rows:
        if prop in NATIVE_ATTRIBUTES:
            continue
        if prop in KNOWN_PDF_TYPOS:
            typos[(entity, prop)] = KNOWN_PDF_TYPOS[prop]
            continue
        held = index.get(entity.lower(), {}).get(prop)
        if not held:
            missing.setdefault((entity, prop), (pdf_type, subtype))
            continue
        want = importer._normalise_type(pdf_type)
        types = set()
        for _s, p in held:
            types.add(p.get("type"))
            types.update(p.get("alt_types") or [])
        if want in types or (want == "Label" and types & {"Label", "Text", "Identifier"}):
            continue
        if (entity, prop, pdf_type) in IGNORED_TYPE_CONFLICTS:
            continue
        conflicts.setdefault((entity, prop), (want, held))
    return missing, conflicts, typos


def apply(catalogue, missing, conflicts, pdf_name):
    changes = []
    for (entity, prop), (pdf_type, subtype) in sorted(missing.items()):
        pset, created = _target_set(catalogue, entity, prop)
        if any(p["name"] == prop for p in pset["properties"]):
            continue
        pset["properties"].append({
            "name": prop, "type": importer._normalise_type(pdf_type), "unit": None,
            "required": False, "controlled": False, "enum_values": [],
            "example_values": [],
            "source": "%s (%s, subtype %s)" % (pdf_name, entity, subtype),
        })
        changes.append("added %s.%s (%s)%s" % (pset["name"], prop, pdf_type,
                                                " in new set" if created else ""))
    for (entity, prop), (want, held) in sorted(conflicts.items()):
        for pset, item in held:
            if item.get("type") == "Unknown":
                item["type"] = want
                changes.append("typed %s.%s as %s" % (pset.get("name") or pset.get(
                    "property_set"), prop, want))
                continue
            alt = item.setdefault("alt_types", [])
            if want not in alt:
                alt.append(want)
                changes.append("accept %s.%s as %s or %s" % (
                    pset.get("name") or pset.get("property_set"), prop, item["type"], want))
    return changes


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf")
    parser.add_argument("--cop", required=True)
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    library = Library.discover(args.root, cop=args.cop)
    with open(library.path, "r", encoding="utf-8") as handle:
        catalogue = json.load(handle)
    text = pdf_text(args.pdf)
    rows = parse_tables(text)
    pairs = set((e, p) for e, _s, p, _t in rows)
    missing, conflicts, typos = reconcile(catalogue, rows)
    pdf_subtypes = parse_subtypes(text)
    new_subtypes = missing_subtypes(library, pdf_subtypes)

    print("COP %s PDF: %d property rows, %d entity/property pairs, %d subtype headings"
          % (args.cop, len(rows), len(pairs), sum(len(v) for v in pdf_subtypes.values())))
    print("Subtypes only in the PDF: %d" % sum(len(v) for v in new_subtypes.values()))
    for entity, tokens in new_subtypes.items():
        print("  %s: %s" % (entity, ", ".join(tokens)))
    print("Missing from catalogue: %d" % len(missing))
    for (entity, prop), (pdf_type, subtype) in sorted(missing.items()):
        print("  %s.%s %s  [subtype %s]" % (entity, prop, pdf_type, subtype))
    print("Datatype differs from catalogue: %d" % len(conflicts))
    for (entity, prop), (want, held) in sorted(conflicts.items()):
        print("  %s.%s PDF %s, catalogue %s" % (
            entity, prop, want, ", ".join("%s:%s" % (s.get("name") or s.get("property_set"),
                                                       p.get("type")) for s, p in held)))
    for (entity, prop), right in sorted(typos.items()):
        print("PDF typo: %s.%s (catalogue: %s)" % (entity, prop, right))
    if not args.apply:
        return 1 if (missing or conflicts or new_subtypes) else 0

    pdf_name = os.path.basename(args.pdf)
    changes = apply(catalogue, missing, conflicts, pdf_name)
    if new_subtypes:
        held = catalogue.setdefault("cop_pdf_subtypes", {})
        for entity, tokens in new_subtypes.items():
            bucket = held.setdefault(entity, [])
            for token in tokens:
                if token not in bucket:
                    bucket.append(token)
                    changes.append("subtype %s.%s from the COP document" % (entity, token))
    with open(args.pdf, "rb") as handle:
        digest = hashlib.sha256(handle.read()).hexdigest()
    previous = catalogue["metadata"].get("cop_pdf_reconciliation") or {}
    if previous.get("sha256") == digest:
        changes = list(previous.get("changes") or []) + [
            c for c in changes if c not in (previous.get("changes") or [])]
    catalogue["metadata"]["cop_pdf_reconciliation"] = {
        "pdf": pdf_name,
        "sha256": digest,
        "reconciled_at": datetime.date.today().isoformat(),
        "pdf_property_pairs": len(pairs),
        "changes_applied": len(changes),
        "pdf_typos": ["%s.%s" % k for k in sorted(typos)],
        "changes": changes,
    }
    importer._atomic_json_dump(library.path, catalogue)
    print("Applied %d change(s) to %s" % (len(changes), library.path))
    for change in changes:
        print("  " + change)
    return 0


if __name__ == "__main__":
    sys.exit(main())
