"""Regression tests for discipline/storey grouping and BCF 2.1 export.

Runs headlessly on stock CPython against a synthetic IFC fixture - no CAD
licence and no real project model required.

    python tools/microstation/tests/featuretest.py
"""

import os
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ifcsg_checker import bcf as bcf_mod            # noqa: E402
from ifcsg_checker import report as report_mod      # noqa: E402
from ifcsg_checker import sources as sources_mod    # noqa: E402
from ifcsg_checker.library import Library           # noqa: E402
from ifcsg_checker.rules import Engine              # noqa: E402
from ifcsg_checker.spf import IfcFile               # noqa: E402

FIXTURE = os.path.join(HERE, "fixture.ifc")
FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def main():
    lib = Library.discover()
    ifc = IfcFile.read(FIXTURE)
    ctx, results = Engine(lib).run(ifc)
    rep = report_mod.Report(sources_mod.browsed_source(FIXTURE), ifc, ctx, results, lib)

    print("Fixture parses")
    check("schema is IFC4", ifc.schema == "IFC4", ifc.schema)
    check("reference view detected",
          any("ReferenceView" in v for v in ifc.view_definitions),
          "; ".join(ifc.view_definitions))
    check("walls found", len(ifc.of_type("IFCWALL")) == 2)

    print("Discipline lookup")
    check("IfcWall is architectural and structural",
          lib.domains_of("IfcWall") == ["ARCHITECTURAL", "STRUCTURAL"],
          str(lib.domains_of("IfcWall")))
    check("IfcColumn keeps its structural domain",
          "STRUCTURAL" in lib.domains_of("IfcColumn"),
          str(lib.domains_of("IfcColumn")))
    check("multi-domain classes group under a joined label",
          lib.domain_of("IfcWall") == "ARCHITECTURAL / STRUCTURAL",
          lib.domain_of("IfcWall"))
    check("unknown class returns empty", lib.domains_of("IfcNotAThing") == [])
    check("catalogue exposes domains", len(lib.domains()) >= 5, str(len(lib.domains())))

    print("Storey resolution")
    by_name = {}
    for cls in ("IFCWALL", "IFCDOOR", "IFCCOLUMN", "IFCSPACE"):
        for ent in ifc.of_type(cls):
            by_name[ent.value(2)] = ent
    check("contained element resolves to its storey",
          ctx.storey_label(by_name["Wall A"].id) == "Level 01",
          ctx.storey_label(by_name["Wall A"].id))
    check("second storey resolves independently",
          ctx.storey_label(by_name["Column A"].id) == "Level 02",
          ctx.storey_label(by_name["Column A"].id))
    check("aggregated space walks up to the storey",
          ctx.storey_label(by_name["Room 1"].id) == "Level 01",
          ctx.storey_label(by_name["Room 1"].id))
    check("unplaced id degrades safely", ctx.storey_label(999999) == "(no storey)")

    print("Breakdown grouping")
    rows = rep.breakdown_rows()
    check("breakdown produced rows", len(rows) > 0, "%d rows" % len(rows))
    check("every row carries a discipline", all(r["discipline"] for r in rows))
    check("every row carries a storey", all(r["storey"] for r in rows))
    check("totals reconcile with the element count",
          sum(r["total"] for r in rows) == len(ctx.element_ids) + len(ifc.of_type("IFCSPACE")),
          "%d vs %d" % (sum(r["total"] for r in rows),
                        len(ctx.element_ids) + len(ifc.of_type("IFCSPACE"))))
    disciplines = sorted(set(r["discipline"] for r in rows))
    check("structural elements are reachable by discipline",
          any("STRUCTURAL" in d for d in disciplines), "; ".join(disciplines))
    check("breakdown reaches the JSON export", "breakdown" in rep.as_dict())

    print("BCF 2.1 export")
    out = os.path.join(tempfile.gettempdir(), "ifcsg_fixture.bcfzip")
    path, topics = bcf_mod.write_bcf(rep, out)
    check("archive written", os.path.isfile(path))
    check("at least one topic", topics > 0, "%d topics" % topics)

    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        check("bcf.version present", "bcf.version" in names)
        version = ET.fromstring(zf.read("bcf.version"))
        check("version is 2.1", version.get("VersionId") == "2.1", version.get("VersionId"))

        markups = [n for n in names if n.endswith("markup.bcf")]
        check("one markup per topic", len(markups) == topics,
              "%d markups, %d topics" % (len(markups), topics))

        guid_seen = False
        for name in markups:
            root = ET.fromstring(zf.read(name))
            topic = root.find("Topic")
            if topic is None or not topic.get("Guid"):
                check("every markup has a Topic with a Guid", False, name)
                break
            if topic.find("Title") is None or not (topic.find("Title").text or "").strip():
                check("every topic has a title", False, name)
                break
        else:
            check("every markup has a Topic with a Guid", True)
            check("every topic has a title", True)

        for name in [n for n in names if n.endswith("viewpoint.bcfv")]:
            root = ET.fromstring(zf.read(name))
            for comp in root.iter("Component"):
                if comp.get("IfcGuid"):
                    guid_seen = True
                    break
            if guid_seen:
                break
        check("viewpoints select elements by IfcGuid", guid_seen)

        oversized = [n for n in names if n.count("/") > 1]
        check("archive is flat: topic folders only", not oversized, ",".join(oversized[:3]))

    os.remove(path)

    print("\nFAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
