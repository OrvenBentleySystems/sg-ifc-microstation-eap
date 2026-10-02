"""Regression harness for every supported catalogue import format."""

import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)

from ifcsg_checker import importer  # noqa: E402
from ifcsg_checker.library import Library  # noqa: E402


FAILURES = []
HEADERS = ["Property Set", "Property Name", "Data Type", "Entity"]
VALUES = ["SGPset_Test", "FireRating", "Text", "IfcWall"]


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def _inline_cell(ref, value):
    return ('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, value))


def _write_xlsx(path):
    headers = [
        "S/N", "Agency", "Identified Component", "Identified parameters",
        "Suggested Revit Representation", "Suggested Archicad Representation",
        "Suggested Tekla Structures Representation",
        "Suggested Bentley OpenBuildings Representation",
        "Suggested Discipline", "IFC4 Entities",
        "IFC Sub Types (* = USERDEFINED)", "Property Set", "Property Name",
        "Property Type", "Property Unit",
        "IFC4 Material Set",
        "Accepted Values (for parameters with Input Limitations)",
    ]
    rows = [
        headers,
        ["1", "BCA", "Test Wall", "Fire rating", "", "", "", "", "ARC",
         "IfcWall", "N.A", "SGPset_Test", "FireRating", "Text", "hr",
         "N.A", "1, 2"],
        ["2", "SCDF", "Test Wall", "Fire rating", "", "", "", "", "ARC",
         "IfcWall", "N.A", "SGPset_Test", "FireRating", "Text", "hr",
         "N.A", "1, 2"],
    ]
    sheet_rows = []
    for row_no, values in enumerate(rows, 1):
        cells = "".join(_inline_cell("%s%d" % (chr(65 + col), row_no), value)
                        for col, value in enumerate(values))
        sheet_rows.append('<row r="%d">%s</row>' % (row_no, cells))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>%s</sheetData></worksheet>' % "".join(sheet_rows))
    workbook = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Mapping" sheetId="1" r:id="rId1"/></sheets></workbook>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet)


def main():
    with tempfile.TemporaryDirectory(prefix="ifcsg-catalogue-") as folder:
        paths = {}
        paths["csv"] = os.path.join(folder, "mapping.csv")
        with open(paths["csv"], "w", encoding="utf-8", newline="") as fh:
            fh.write(",".join(HEADERS) + "\n" + ",".join(VALUES) + "\n")

        paths["md"] = os.path.join(folder, "mapping.md")
        with open(paths["md"], "w", encoding="utf-8") as fh:
            fh.write("| %s |\n" % " | ".join(HEADERS))
            fh.write("| --- | --- | --- | --- |\n")
            fh.write("| %s |\n" % " | ".join(VALUES))

        paths["txt"] = os.path.join(folder, "mapping.txt")
        with open(paths["txt"], "w", encoding="utf-8") as fh:
            fh.write("PropertySet:\tSGPset_Test\tI\tIfcWall\n")
            fh.write("FireRating\tText\n")

        paths["json"] = os.path.join(folder, "mapping.json")
        with open(paths["json"], "w", encoding="utf-8") as fh:
            json.dump({"sgpsets": [{
                "name": "SGPset_Test", "entities": ["IfcWall"],
                "properties": [{"name": "FireRating", "type": "Text"}],
            }]}, fh)

        paths["xlsx"] = os.path.join(folder, "mapping.xlsx")
        _write_xlsx(paths["xlsx"])

        print("Supported input formats")
        for kind in ("csv", "md", "txt", "json", "xlsx"):
            parsed = importer.parse(paths[kind])
            ok = (len(parsed) == 1 and parsed[0]["name"] == "SGPset_Test"
                  and parsed[0]["properties"][0]["name"] == "FireRating")
            check("%s importer" % kind.upper(), ok, str(parsed[:1]))
        xlsx = importer.parse(paths["xlsx"])
        check("official workbook headings select Property Type",
              xlsx[0]["properties"][0]["type"] == "Label",
              xlsx[0]["properties"][0]["type"])
        check("duplicate workbook rows merge to one property",
              len(xlsx[0]["properties"]) == 1,
              str(len(xlsx[0]["properties"])))
        sections = importer.xlsx_sections(paths["xlsx"])
        check("official workbook headings produce identified components",
              len(sections["identified_components"]["items"]) == 1)

        print("Transactional merge")
        test_root = os.path.join(folder, "library")
        shutil.copytree(os.path.join(ROOT, "data"), os.path.join(test_root, "data"))
        summary = importer.merge(
            test_root, paths["csv"], mapping_edition="2026-09-04")
        generated = os.path.join(test_root, "data", "catalogues", "cop-4.json")
        check("merge updates the newest COP catalogue",
              os.path.isfile(generated) and summary["catalogue"] == generated,
              summary.get("catalogue", ""))
        check("merge reports imported set",
              summary["sets_total"] >= 1 and summary["properties_total"] >= 1,
              str(summary))
        leftovers = [name for name in os.listdir(os.path.dirname(generated))
                     if name.endswith(".tmp")]
        check("atomic merge leaves no temporary files", not leftovers, str(leftovers))
        library = Library.discover(test_root)
        check("updated catalogue reloads",
              library.editions["mapping_edition"] == "2026-09-04")

    print("\nFAILURES: %d" % len(FAILURES))
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
