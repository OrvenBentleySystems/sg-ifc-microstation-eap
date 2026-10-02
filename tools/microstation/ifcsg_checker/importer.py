"""Import an IFC+SG property mapping from whatever format the authority published.

Accepts .xlsx, .csv, .json, .md/.markdown and .txt. Every reader is standard
library only, including the spreadsheet reader, so the tool keeps working on a
machine that has nothing but MicroStation installed.

Importing merges into the existing catalogue rather than replacing it, and every
source is recorded in the provenance so a report can always state where a given
property definition came from.
"""

import csv
import datetime
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET

from . import __version__

SUPPORTED_EXTENSIONS = (".xlsx", ".csv", ".json", ".md", ".markdown", ".txt")

# Column headings seen across BCA, vendor and third-party exports.
HEADER_ALIASES = {
    "pset": ("property set", "propertyset", "property_set", "pset", "sgpset",
             "set name", "setname", "property set name"),
    "prop": ("property name", "propertyname", "property", "parameter name",
             "parameter", "attribute", "name", "field"),
    "type": ("property type", "data type", "datatype", "data_type", "type",
             "value type", "valuetype", "ifc data type"),
    "entity": ("ifc4 entities", "ifc entity", "ifcentity", "entity", "ifc class", "ifcclass",
               "class", "ifc element", "applicable entity", "applicability"),
    "binding": ("binding", "instance/type", "instance or type", "i/t", "level"),
    "values": ("accepted values for parameters with input limitations",
               "enumeration", "enumeration values", "controlled values",
               "value list", "values", "allowed values", "sample values",
               "example values", "controlled"),
    "subtype": ("ifc sub types", "subtype", "predefined type",
                "predefinedtype", "object type",
                "objecttype", "token"),
    "unit": ("property unit", "unit", "units"),
    "agency": ("agency", "agencies"),
    "discipline": ("suggested discipline", "discipline", "disciplines"),
    "component": ("identified component", "component"),
}

# Normalised to the vocabulary accepted by data/catalogues/*.json.
TYPE_ALIASES = {
    "label": "Label", "text": "Label", "string": "Label", "str": "Label",
    "ifclabel": "Label", "ifctext": "Label",
    "identifier": "Identifier", "ifcidentifier": "Identifier",
    "boolean": "Boolean", "bool": "Boolean", "yes/no": "Boolean",
    "ifcboolean": "Boolean", "logical": "Boolean",
    "integer": "Integer", "int": "Integer", "whole number": "Integer",
    "ifcinteger": "Integer",
    "real": "Real", "number": "Real", "double": "Real", "float": "Real",
    "decimal": "Real", "ifcreal": "Real", "ratio": "Real",
    "length": "Length", "ifclengthmeasure": "Length", "distance": "Length",
    "area": "Area", "ifcareameasure": "Area",
    "volume": "Volume", "ifcvolumemeasure": "Volume",
    "count": "Count", "ifccountmeasure": "Count",
    "mass": "Mass", "time": "Time", "date": "Label",
}


class ImportError_(Exception):
    pass


# ---------------------------------------------------------------------------
# Spreadsheet reader (no openpyxl)
# ---------------------------------------------------------------------------

_SS_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _col_index(ref):
    """'BC12' -> 54 (zero based column)."""
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


def read_xlsx(path):
    """Return {sheet name: [[cell, ...], ...]} using only the standard library."""
    sheets = {}
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall(_SS_NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(_SS_NS + "t")))

        targets = []
        if "xl/workbook.xml" in names:
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            relmap = {}
            if "xl/_rels/workbook.xml.rels" in names:
                rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                for rel in rels:
                    relmap[rel.get("Id")] = rel.get("Target")
            holder = wb.find(_SS_NS + "sheets")
            for sheet in (holder if holder is not None else []):
                target = relmap.get(sheet.get(_REL_NS + "id"), "")
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = "xl/" + target
                targets.append((sheet.get("name") or target, target))
        if not targets:
            targets = [(n, n) for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")]

        for label, target in targets:
            if target not in names:
                continue
            rows = []
            root = ET.fromstring(zf.read(target))
            for row in root.iter(_SS_NS + "row"):
                cells = []
                for cell in row.iter(_SS_NS + "c"):
                    idx = _col_index(cell.get("r") or "")
                    while len(cells) < idx:
                        cells.append("")
                    kind = cell.get("t")
                    value = ""
                    if kind == "inlineStr":
                        holder = cell.find(_SS_NS + "is")
                        if holder is not None:
                            value = "".join(t.text or "" for t in holder.iter(_SS_NS + "t"))
                    else:
                        node = cell.find(_SS_NS + "v")
                        raw = node.text if node is not None else ""
                        if kind == "s" and raw not in (None, ""):
                            try:
                                value = shared[int(raw)]
                            except (ValueError, IndexError):
                                value = ""
                        else:
                            value = raw or ""
                    cells.append(value)
                rows.append(cells)
            sheets[label] = rows
    return sheets


# ---------------------------------------------------------------------------
# Table interpretation
# ---------------------------------------------------------------------------

def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").strip().lower()).strip()


def _match_header(cell):
    value = _norm(cell)
    if not value:
        return None
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if value == _norm(alias):
                return key
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if _norm(alias) in value:
                return key
    return None


def _find_header_row(rows, look=40):
    """Row index and column map of the first row that looks like a header."""
    best = None
    for i, row in enumerate(rows[:look]):
        mapping = {}
        # Exact matches win across the complete row. This prevents an early
        # "IFC Sub Types" column from fuzzy-matching the generic alias "type"
        # before the exact "Property Type" column is seen.
        for j, cell in enumerate(row):
            value = _norm(cell)
            key = next(
                (candidate for candidate, aliases in HEADER_ALIASES.items()
                 if any(value == _norm(alias) for alias in aliases)),
                None)
            if key and key not in mapping:
                mapping[key] = j
        for j, cell in enumerate(row):
            key = _match_header(cell)
            if key and key not in mapping:
                mapping[key] = j
        if "prop" in mapping and len(mapping) >= 2:
            score = len(mapping)
            if best is None or score > best[2]:
                best = (i, mapping, score)
    return (best[0], best[1]) if best else (None, None)


def _cell(row, index):
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _normalise_type(raw):
    value = _norm(raw)
    if not value:
        return None
    if value in TYPE_ALIASES:
        return TYPE_ALIASES[value]
    for key, mapped in TYPE_ALIASES.items():
        if key in value:
            return mapped
    return "Unknown"


def _split_values(raw):
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[;\n|]+", text) if any(c in text for c in ";\n|") else text.split(",")
    return [
        part.strip() for part in parts
        if part.strip() and _norm(part) not in ("n a", "na", "not applicable", "-")
    ]


def _valid_pset_name(name):
    return str(name or "").startswith(("SGPset_", "Pset_", "Qto_"))


def _is_placeholder(value):
    return _norm(value) in ("", "n a", "na", "not applicable", "none", "-")


def _prefer_type(current, incoming):
    if not current or current == "Unknown":
        return incoming or "Unknown"
    if not incoming or incoming == "Unknown" or incoming == current:
        return current
    if current == "Label" and incoming != "Label":
        return incoming
    return current


def rows_to_psets(rows, origin=""):
    """Turn a header-bearing table into property set definitions."""
    header_row, columns = _find_header_row(rows)
    if header_row is None:
        return []

    out = {}
    current = None
    for row in rows[header_row + 1:]:
        if not any(str(c or "").strip() for c in row):
            continue
        pset = _cell(row, columns.get("pset"))
        if pset:
            current = pset if _valid_pset_name(pset) else None
        if not current:
            continue
        prop = _cell(row, columns.get("prop"))
        entry = out.setdefault(current, {
            "name": current,
            "binding": _cell(row, columns.get("binding")) or "I",
            "entities": [],
            "subtypes": [],
            "disciplines": [],
            "agencies": [],
            "gateways": [],
            "verified": True,
            "source": origin,
            "properties": [],
        })
        entity = _cell(row, columns.get("entity"))
        for token in re.split(r"[,;/\s]+", entity):
            token = token.strip()
            if token.lower().startswith("ifc") and token not in entry["entities"]:
                entry["entities"].append(token)
        subtype = _cell(row, columns.get("subtype"))
        if subtype and subtype not in entry["subtypes"]:
            entry["subtypes"].append(subtype)
        if not prop or prop == current or _is_placeholder(prop):
            continue
        for field in ("agency", "discipline"):
            target = "agencies" if field == "agency" else "disciplines"
            for token in _split_values(_cell(row, columns.get(field))):
                if token not in entry[target]:
                    entry[target].append(token)
        values = _split_values(_cell(row, columns.get("values")))
        incoming_type = _normalise_type(_cell(row, columns.get("type"))) or "Label"
        existing = next(
            (item for item in entry["properties"] if item.get("name") == prop),
            None)
        if existing is not None:
            existing["type"] = _prefer_type(existing.get("type"), incoming_type)
            existing["controlled"] = bool(existing.get("controlled") or values)
            for value in values:
                if value not in existing["enum_values"]:
                    existing["enum_values"].append(value)
            if not existing.get("unit"):
                existing["unit"] = _cell(row, columns.get("unit")) or None
            continue
        entry["properties"].append({
            "name": prop,
            "type": incoming_type,
            "unit": _cell(row, columns.get("unit")) or None,
            "required": False,
            "controlled": bool(values),
            "enum_values": values,
            "example_values": [],
        })
    return [v for v in out.values() if v["properties"]]


# ---------------------------------------------------------------------------
# Per-format readers
# ---------------------------------------------------------------------------

def _from_xlsx(path):
    psets = []
    for sheet, rows in read_xlsx(path).items():
        psets.extend(rows_to_psets(rows, origin="%s#%s" % (os.path.basename(path), sheet)))
    return psets


def _from_csv(path):
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = [row for row in csv.reader(fh, dialect)]
    return rows_to_psets(rows, origin=os.path.basename(path))


def _from_markdown(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c):
            continue          # the ---|--- separator row
        rows.append(cells)
    return rows_to_psets(rows, origin=os.path.basename(path))


def _looks_like_pset(record):
    if not isinstance(record, dict):
        return False
    if not record.get("name"):
        return False
    props = record.get("properties")
    return isinstance(props, list)


def _find_pset_records(payload, depth=0):
    """Locate the property-set list wherever a publisher chose to nest it."""
    if depth > 6:
        return None
    if isinstance(payload, list):
        if payload and all(_looks_like_pset(r) for r in payload if isinstance(r, dict)):
            hits = [r for r in payload if _looks_like_pset(r)]
            if hits:
                return hits
        return None
    if not isinstance(payload, dict):
        return None

    # Named containers first, so a nested "sets" wins over an unrelated list.
    for key in ("sgpsets", "property_sets", "propertySets", "psets", "sets",
                "definitions", "items"):
        if key in payload:
            found = _find_pset_records(payload[key], depth + 1)
            if found:
                return found
    for value in payload.values():
        found = _find_pset_records(value, depth + 1)
        if found:
            return found
    return None


def _normalise_pset(record, origin):
    """Fill in the fields the catalogue expects, whatever the source omitted."""
    props = []
    for prop in record.get("properties") or []:
        if isinstance(prop, str):
            prop = {"name": prop}
        if (not isinstance(prop, dict) or not prop.get("name")
                or _is_placeholder(prop.get("name"))):
            continue
        values = prop.get("enum_values") or prop.get("values") or []
        if isinstance(values, str):
            values = _split_values(values)
        props.append({
            "name": prop["name"],
            "type": _normalise_type(prop.get("type")) or "Label",
            "unit": prop.get("unit"),
            "required": bool(prop.get("required")),
            "controlled": bool(prop.get("controlled") or values),
            "enum_values": list(values),
            "example_values": list(prop.get("example_values") or []),
            "note": prop.get("note"),
        })
    entities = record.get("entities") or record.get("entity") or []
    if isinstance(entities, str):
        entities = [entities]
    return {
        "name": record["name"],
        "binding": record.get("binding") or "I",
        "entities": list(entities),
        "subtypes": list(record.get("subtypes") or []),
        "agencies": list(record.get("agencies") or []),
        "verified": bool(record.get("verified", True)),
        "source": record.get("source") or origin,
        "properties": props,
    }


def _from_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ImportError_(
            "%s is not valid JSON: %s at line %d, column %d."
            % (os.path.basename(path), exc.msg, exc.lineno, exc.colno))
    except OSError as exc:
        raise ImportError_("Could not read %s: %s" % (os.path.basename(path), exc))

    records = _find_pset_records(payload)
    if records:
        origin = os.path.basename(path)
        return [_normalise_pset(r, origin) for r in records
                if _normalise_pset(r, origin)["properties"]]

    # Fall back to a flat table of records, e.g. rows exported from a spreadsheet.
    rows = payload if isinstance(payload, list) else None
    if rows is None and isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                rows = value
                break
    if not rows:
        raise ImportError_(
            "No property sets found in %s. Expected either a list of records with "
            "'name' and 'properties', or a flat table of rows."
            % os.path.basename(path))
    keys = list(rows[0].keys())
    return rows_to_psets([keys] + [[r.get(k, "") for k in keys] for r in rows],
                         origin=os.path.basename(path))


# Sections beyond the property sets that a consolidated library may carry.
def json_sections(path):
    """Editions and reference blocks from a consolidated IFC+SG library file."""
    out = {"mapping_edition": None, "cop_edition": None, "area_schemes": None,
           "identified_components": None, "validation_rules": None,
           "entity_domains": None, "title": None, "version": None}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            payload = json.load(fh)
    except Exception:
        return out
    if not isinstance(payload, dict):
        return out

    meta = payload.get("meta") or {}
    aligned = meta.get("aligned_to") or {}
    provenance = payload.get("provenance") or {}
    out["title"] = meta.get("title")
    out["version"] = meta.get("version")
    out["mapping_edition"] = (aligned.get("mapping_edition")
                              or provenance.get("mapping_edition")
                              or meta.get("mapping_edition"))
    out["cop_edition"] = (aligned.get("cop_edition")
                          or provenance.get("cop_edition")
                          or meta.get("cop_edition"))

    spaces = payload.get("space_area_schemes") or payload.get("spaces") or {}
    schemes = spaces.get("schemes") if isinstance(spaces, dict) else None
    if isinstance(schemes, list) and schemes:
        normalised = []
        for scheme in schemes:
            item = dict(scheme)
            # The consolidated library calls them example_values.
            if "example_values" in item and "sample_values" not in item:
                item["sample_values"] = item.get("example_values")
            normalised.append(item)
        out["area_schemes"] = {
            "id": "ifcsg.space.areas",
            "title": "IFC+SG Space and Area Schemes (imported)",
            "verified": True,
            "concept": spaces.get("concept"),
            "carrier": spaces.get("carrier"),
            "rules": spaces.get("rules"),
            "area_schemes": normalised,
            "other_space_properties": spaces.get("other_space_properties"),
        }

    ics = payload.get("identified_components") or {}
    items = ics.get("items") if isinstance(ics, dict) else ics
    if isinstance(items, list) and items:
        out["identified_components"] = {"items": items}

    validation = payload.get("validation") or {}
    rules = validation.get("rules") if isinstance(validation, dict) else None
    if isinstance(rules, list) and rules:
        out["validation_rules"] = {
            "id": "ifcsg.validation",
            "title": "IFC+SG validation rules (imported)",
            "official_model_checker": validation.get("official_model_checker"),
            "third_party_validators": validation.get("third_party_validators"),
            "validator_caveat": validation.get("validator_caveat"),
            "rule_set": {"rules": rules},
            "common_failure_patterns": validation.get("common_failure_patterns"),
        }

    entities = payload.get("entities") or {}
    domains = entities.get("domains") if isinstance(entities, dict) else None
    if isinstance(domains, list) and domains:
        out["entity_domains"] = {"id": "ifcsg.entities", "domains": domains,
                                 "subtype_guidance": entities.get("subtype_guidance"),
                                 "element_representation_rule":
                                     entities.get("element_representation_rule")}
    return out


def xlsx_sections(path):
    """Supplemental component and controlled-value blocks from the BCA workbook."""
    out = {}
    components = {}
    space_values = {}
    for _sheet, rows in read_xlsx(path).items():
        header_row, columns = _find_header_row(rows)
        if header_row is not None and all(
                key in columns for key in ("component", "entity", "pset")):
            for row in rows[header_row + 1:]:
                name = _cell(row, columns.get("component"))
                entity = _cell(row, columns.get("entity"))
                pset = _cell(row, columns.get("pset"))
                if not name or not entity or not _valid_pset_name(pset):
                    continue
                key = (name, entity)
                item = components.setdefault(key, {
                    "name": name,
                    "entity": entity,
                    "subtypes": [],
                    "property_sets": [],
                    "disciplines": [],
                    "agencies": [],
                })
                subtype = _cell(row, columns.get("subtype"))
                if subtype and _norm(subtype) not in ("n a", "na"):
                    for token in _split_values(subtype):
                        if token not in item["subtypes"]:
                            item["subtypes"].append(token)
                if pset not in item["property_sets"]:
                    item["property_sets"].append(pset)
                for field, target in (
                        ("discipline", "disciplines"), ("agency", "agencies")):
                    for token in _split_values(_cell(row, columns.get(field))):
                        if token not in item[target]:
                            item[target].append(token)
            continue

        if rows and len(rows[0]) >= 2 and _norm(rows[0][0]) == "property":
            for row in rows[1:]:
                prop = _cell(row, 0)
                value = _cell(row, 1)
                if prop and value:
                    bucket = space_values.setdefault(prop, [])
                    if value not in bucket:
                        bucket.append(value)
            continue

        # "<Property Words> Enum" sheets carry the controlled list for a single
        # property, e.g. "Industrial Activity Type Enum" -> IndustrialActivityType.
        if _sheet.strip().lower().endswith(" enum") and rows and len(rows[0]) >= 2:
            prop = re.sub(r"[^A-Za-z0-9]", "", _sheet.strip()[:-len(" enum")].title())
            column = next((j for j, cell in enumerate(rows[0])
                           if "enum" in _norm(cell) or "input" in _norm(cell)), 1)
            values = [_cell(row, column) for row in rows[1:] if _cell(row, column)]
            if prop and values:
                space_values[prop] = list(dict.fromkeys(values))
    if components:
        out["identified_components"] = {
            "items": sorted(
                components.values(),
                key=lambda item: (item["name"], item["entity"]))
        }
    if space_values:
        out["space_values"] = space_values
    return out


def peek_editions(path):
    """Mapping and COP edition declared inside the file, if any."""
    if os.path.splitext(path)[1].lower() != ".json":
        return (None, None)
    sections = json_sections(path)
    return (sections["mapping_edition"], sections["cop_edition"])


def _from_revit_txt(path):
    """PropertySet:<tab>Name<tab>binding<tab>Entity, then Property<tab>Type lines."""
    out = {}
    current = None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("\t") if p.strip() != ""]
            if not parts:
                continue
            if parts[0].lower().startswith("propertyset"):
                name = parts[1] if len(parts) > 1 else None
                if not name:
                    current = None
                    continue
                # A set can be declared more than once, once per applicable entity.
                current = out.setdefault(name, {
                    "name": name,
                    "binding": parts[2] if len(parts) > 2 else "I",
                    "entities": [],
                    "subtypes": [],
                    "verified": True,
                    "source": os.path.basename(path),
                    "properties": [],
                })
                for token in parts[3:]:
                    if token.lower().startswith("ifc") and token not in current["entities"]:
                        current["entities"].append(token)
            elif (current is not None and not set(parts[0]) <= set("-=")
                  and not _is_placeholder(parts[0])):
                existing = {p["name"] for p in current["properties"]}
                if parts[0] in existing:
                    continue
                current["properties"].append({
                    "name": parts[0],
                    "type": _normalise_type(parts[1]) if len(parts) > 1 else "Label",
                    "unit": None,
                    "required": False,
                    "controlled": False,
                    "enum_values": [],
                    "example_values": [],
                })
    return [p for p in out.values() if p["properties"]]


READERS = {
    ".xlsx": _from_xlsx,
    ".csv": _from_csv,
    ".json": _from_json,
    ".md": _from_markdown,
    ".markdown": _from_markdown,
    ".txt": _from_revit_txt,
}


def parse(path):
    if not os.path.isfile(path):
        raise ImportError_("File not found: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    reader = READERS.get(ext)
    if reader is None:
        raise ImportError_("Unsupported file type '%s'. Supported: %s"
                           % (ext, ", ".join(SUPPORTED_EXTENSIONS)))
    try:
        psets = reader(path)
    except ImportError_:
        raise
    except zipfile.BadZipFile:
        raise ImportError_("%s is not a readable .xlsx workbook. If it is an older "
                           ".xls file, save it as .xlsx or export it as .csv first."
                           % os.path.basename(path))
    except UnicodeDecodeError as exc:
        raise ImportError_("%s could not be decoded as text: %s"
                           % (os.path.basename(path), exc))
    except Exception as exc:
        raise ImportError_("Could not read %s (%s): %s"
                           % (os.path.basename(path), type(exc).__name__, exc))
    if not psets:
        raise ImportError_(
            "No property sets recognised in %s. The importer looks for a header row "
            "naming at least a property column plus a property-set, data-type or "
            "entity column, or a JSON list of records with 'name' and 'properties'."
            % os.path.basename(path))
    return psets


# ---------------------------------------------------------------------------
# Merge into the catalogue
# ---------------------------------------------------------------------------

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _catalogue_path(library_root):
    """Newest catalogue under library_root, or the legacy single file."""
    from .library import Library, catalogue_paths
    if not catalogue_paths(library_root):
        raise ImportError_("No catalogue under %s" % os.path.join(
            library_root, "data", "catalogues"))
    return Library.available(library_root)[0].path


def _atomic_json_dump(path, payload):
    """Replace a generated JSON file only after the complete payload is durable."""
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    fd, temp_path = tempfile.mkstemp(prefix=".%s." % os.path.basename(path),
                                     suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def load_catalogue(library_root, catalogue_path=None):
    path = catalogue_path or _catalogue_path(library_root)
    if not os.path.isfile(path):
        raise ImportError_("Catalogue not found: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("format") != "ifcsg-checker-catalogue":
        raise ImportError_("%s is not an IFC+SG checker catalogue." % path)
    return payload


def _property_keys(psets):
    return set((item["name"], prop["name"])
               for item in psets for prop in item.get("properties") or [])


def merge(library_root, path, mapping_edition=None, cop_edition=None,
          replace=False, log=None, catalogue_path=None, previous=None):
    """Import path into a catalogue. Returns a summary dict.

    previous: the prior official workbook. Property definitions it published
    that path no longer publishes are removed, so a renamed or withdrawn
    property is reported instead of silently accepted.
    """
    say = log or (lambda _m: None)
    target_path = catalogue_path or _catalogue_path(library_root)
    say("Reading %s ..." % os.path.basename(path))
    incoming = parse(path)
    say("Recognised %d property set(s), %d property definition(s)."
        % (len(incoming), sum(len(p.get("properties") or []) for p in incoming)))

    extension = os.path.splitext(path)[1].lower()
    authoritative = extension == ".xlsx"
    if extension == ".json":
        sections = json_sections(path)
    elif extension == ".xlsx":
        sections = xlsx_sections(path)
    else:
        sections = {}
    mapping_edition = mapping_edition or sections.get("mapping_edition")
    cop_edition = cop_edition or sections.get("cop_edition")
    controlled_values = sections.get("space_values", {})
    for entry in incoming:
        for prop in entry.get("properties", []):
            values = controlled_values.get(prop.get("name"))
            if values:
                prop["enum_values"] = list(values)
                prop["controlled"] = True
            elif any(str(v).lower().startswith("refer to")
                     for v in prop.get("enum_values") or []):
                # A pointer to another sheet is not an accepted value.
                prop["enum_values"] = []

    catalogue = load_catalogue(library_root, target_path)
    if replace:
        catalogue["property_sets"] = []
        catalogue.setdefault("metadata", {})["sources"] = []
        say("Existing catalogue cleared before import.")

    removed = []
    if previous:
        say("Comparing with previous workbook %s ..." % os.path.basename(previous))
        gone = _property_keys(parse(previous)) - _property_keys(incoming)
        for item in catalogue.get("property_sets", []):
            keep = []
            for prop in item.get("properties") or []:
                if (item.get("name"), prop.get("name")) in gone:
                    removed.append("%s.%s" % (item.get("name"), prop.get("name")))
                else:
                    keep.append(prop)
            item["properties"] = keep
        catalogue["property_sets"] = [
            item for item in catalogue.get("property_sets", [])
            if item.get("properties")]
        say("Removed %d property definition(s) withdrawn by the new workbook."
            % len(removed))

    existing = dict(
        (item["name"], item)
        for item in catalogue.get("property_sets", [])
        if item.get("name"))
    added_sets = 0
    updated_sets = 0
    added_props = 0
    retyped_props = 0
    enriched_props = 0
    conflicts = []

    for entry in incoming:
        name = entry.get("name")
        if not name:
            continue
        target = existing.get(name)
        if target is None:
            existing[name] = entry
            added_sets += 1
            added_props += len(entry.get("properties") or [])
            continue

        updated = False
        by_name = dict((p.get("name"), p) for p in target.get("properties") or [])
        for prop in entry.get("properties") or []:
            pname = prop.get("name")
            if not pname:
                continue
            current = by_name.get(pname)
            if current is None:
                target.setdefault("properties", []).append(prop)
                by_name[pname] = prop
                added_props += 1
                updated = True
                continue
            if prop.get("type") and current.get("type") != prop.get("type"):
                conflicts.append("%s.%s: %s -> %s"
                                 % (name, pname, current.get("type"), prop.get("type")))
                current["type"] = prop["type"]
                retyped_props += 1
                updated = True
            if prop.get("enum_values") and (
                    not current.get("enum_values")
                    or (authoritative and current.get("enum_values") != prop["enum_values"])):
                current["enum_values"] = prop["enum_values"]
                current["controlled"] = True
                enriched_props += 1
                updated = True
            elif prop.get("controlled") and not current.get("controlled"):
                current["controlled"] = True
                enriched_props += 1
                updated = True
        for key in ("entities", "subtypes", "agencies"):
            for value in entry.get(key) or []:
                if value not in (target.get(key) or []):
                    target.setdefault(key, []).append(value)
                    updated = True
        if updated:
            updated_sets += 1

    catalogue["property_sets"] = [existing[key] for key in sorted(existing)]

    provenance = catalogue.setdefault("metadata", {})
    if mapping_edition:
        provenance["mapping_edition"] = mapping_edition
    if cop_edition:
        provenance["cop_edition"] = cop_edition
    provenance.setdefault("mapping_edition", "unknown")
    provenance.setdefault("cop_edition", "unknown")
    provenance["built_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    provenance["builder_version"] = "importer %s" % __version__
    sources = provenance.setdefault("sources", [])
    sources[:] = [source for source in sources
                  if source.get("name") != os.path.basename(path)]
    sources.append({
        "name": os.path.basename(path),
        "sha256": sha256(path),
        "kind": os.path.splitext(path)[1].lower().lstrip("."),
        "imported_at": provenance["built_at"],
    })
    provenance["authoritative_mapping"] = any(
        source.get("kind") == "xlsx" for source in sources)
    provenance["complete"] = True

    extras = []
    for key, target, label in (
            ("area_schemes", "area_schemes", "area schemes"),
            ("identified_components", "identified_components", "identified components"),
            ("validation_rules", "rules", "validation rules"),
            ("entity_domains", "entity_domains", "entity catalogue")):
        block = sections.get(key)
        if not block:
            continue
        if key == "area_schemes":
            value = block.get("area_schemes") or []
        elif key == "validation_rules":
            value = (block.get("rule_set") or {}).get("rules") or []
        elif key == "entity_domains":
            value = block.get("domains") or []
        else:
            value = block.get("items") or []
        catalogue[target] = value
        count = len(value)
        extras.append("%s (%d)" % (label, count))
        say("Imported %s: %d entries." % (label, count))

    summary = {
        "file": os.path.basename(path),
        "sets_total": len(catalogue["property_sets"]),
        "properties_total": sum(
            len(item.get("properties") or [])
            for item in catalogue["property_sets"]),
        "sets_added": added_sets,
        "sets_updated": updated_sets,
        "properties_added": added_props,
        "properties_retyped": retyped_props,
        "properties_enriched": enriched_props,
        "properties_removed": len(removed),
        "removed": removed,
        "conflicts": conflicts[:50],
        "mapping_edition": provenance["mapping_edition"],
        "cop_edition": provenance["cop_edition"],
        "extras": extras,
        "catalogue": target_path,
    }
    provenance["build_summary"] = {
        "property_sets": summary["sets_total"],
        "properties": summary["properties_total"],
        "sets_added": summary["sets_added"],
        "sets_updated": summary["sets_updated"],
        "properties_added": summary["properties_added"],
        "properties_retyped": summary["properties_retyped"],
        "properties_removed": len(removed),
        "datatype_conflicts": len(conflicts),
    }
    if previous:
        provenance["removed_properties"] = sorted(removed)
        provenance["previous_mapping"] = {
            "name": os.path.basename(previous), "sha256": sha256(previous)}
    _atomic_json_dump(target_path, catalogue)

    say("Catalogue now holds %d property sets and %d properties."
        % (summary["sets_total"], summary["properties_total"]))
    if added_sets or updated_sets:
        say("Added %d set(s), updated %d, added %d property definition(s), retyped %d."
            % (added_sets, updated_sets, added_props, retyped_props))
    if conflicts:
        say("%d datatype conflict(s) resolved in favour of the import." % len(conflicts))
    return summary
