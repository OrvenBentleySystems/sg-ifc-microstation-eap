"""COP edition selection, COP 4 content and edition-to-edition rebuilds."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)

from ifcsg_checker.library import Library, cop_key  # noqa: E402
from ifcsg_checker.rules import Engine, PASS, WARN  # noqa: E402
from ifcsg_checker.spf import IfcFile  # noqa: E402
from ifcsg_checker import report as report_mod  # noqa: E402
from ifcsg_checker import sources as sources_mod  # noqa: E402
from cataloguetest import _write_xlsx  # noqa: E402

FAILURES = []
FIXTURE = os.path.join(HERE, "fixture.ifc")


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def _props(library, pset):
    entry = library.sgpsets.get(pset) or {}
    return dict((p["name"], p) for p in entry.get("properties", []))


def editions():
    print("Edition discovery")
    found = Library.available(ROOT)
    cops = [item.cop_edition for item in found]
    check("both editions installed, newest first", cops[:2] == ["4", "3.1"], str(cops))
    check("cop keys order numerically", cop_key("10") > cop_key("4") > cop_key("3.1"))
    default = Library.discover(ROOT)
    check("newest edition is the default", default.editions["cop_edition"] == "4")
    check("newest edition is not superseded", not default.editions["superseded"])
    old = Library.discover(ROOT, cop="3.1")
    check("older edition loads on request", old.editions["cop_edition"] == "3.1")
    check("older edition is flagged superseded",
          old.editions["superseded"] and old.editions["latest_cop"] == "4")
    try:
        Library.discover(ROOT, cop="99")
        check("unknown edition is rejected", False)
    except ValueError as exc:
        check("unknown edition is rejected", "not installed" in str(exc))

    with tempfile.TemporaryDirectory(prefix="ifcsg-legacy-") as folder:
        os.makedirs(os.path.join(folder, "data"))
        shutil.copyfile(os.path.join(ROOT, "data", "catalogues", "cop-3.1.json"),
                        os.path.join(folder, "data", "catalogue.json"))
        legacy = Library.discover(folder)
        check("legacy single-file layout still loads",
              legacy.editions["cop_edition"] == "3.1" and not legacy.is_superseded)


def cop4_content():
    print("COP 4 catalogue content")
    lib = Library.discover(ROOT, cop="4")
    meta = lib.metadata
    check("mapping edition is the September 2026 workbook",
          meta["mapping_edition"] == "2026-09-25")
    check("COP 4 publication recorded", meta.get("cop_published") == "2026-09")
    check("workbook source recorded",
          any(s["name"] == "industry-mapping-09-2026.xlsx" for s in meta["sources"]))
    check("withdrawn SGPset_Door.SelfClosing removed",
          "SelfClosing" not in _props(lib, "SGPset_Door"))
    check("SelfClosing now in Pset_DoorCommon",
          "SelfClosing" in _props(lib, "Pset_DoorCommon"))
    check("IsExternal moved to Pset_WallCommon",
          "IsExternal" in _props(lib, "Pset_WallCommon")
          and "IsExternal" not in _props(lib, "SGPset_Wall"))
    check("DoubleBayFacade renamed", "DoubleBayFacade" in _props(lib, "SGPset_Wall")
          and "DoubleBay\u0046a\u00e7ade" not in _props(lib, "SGPset_Wall"))
    activity = _props(lib, "SGPset_Space").get("IndustrialActivityType", {})
    values = activity.get("enum_values", [])
    check("IndustrialActivityType enum resolved from its sheet",
          len(values) > 100 and values[0].startswith("SS593-AnnexA")
          and not any(v.lower().startswith("refer to") for v in values),
          "%d values" % len(values))
    check("COP 4 adds AmendmentStatus", "AmendmentStatus" in _props(lib, "SGPset_Wall"))
    check("COP 4 retypes SGPset_Pump.PumpHead",
          _props(lib, "SGPset_Pump")["PumpHead"]["type"] == "Length")
    check("identified components replaced by the workbook",
          len(lib.identified_components) == 132, str(len(lib.identified_components)))
    old = Library.discover(ROOT, cop="3.1")
    check("COP 3.1 is unchanged by the COP 4 build",
          "SelfClosing" in _props(old, "SGPset_Door")
          and old.editions["mapping_edition"] == "2025-12-04")


def checks_follow_edition():
    print("Checks follow the selected edition")
    ifc = IfcFile.read(FIXTURE)
    source = sources_mod.browsed_source(FIXTURE)
    out = {}
    for cop in ("4", "3.1"):
        lib = Library.discover(ROOT, cop=cop)
        ctx, results = Engine(lib).run(ifc)
        rep = report_mod.Report(source, ifc, ctx, results, lib)
        out[cop] = (rep, dict((r.rule_id, r) for r in results))
    check("same parsed IFC re-runs against a second edition",
          bool(out["4"][1]) and bool(out["3.1"][1]))
    check("DOC.001 passes on the newest edition",
          out["4"][1]["DOC.001"].status == PASS, out["4"][1]["DOC.001"].status)
    check("DOC.001 warns on a superseded edition",
          out["3.1"][1]["DOC.001"].status == WARN
          and "superseded" in out["3.1"][1]["DOC.001"].summary)
    header = out["3.1"][0].header()
    check("report header stamps the edition",
          header["cop_edition"] == "3.1" and header["cop_superseded"] is True)
    check("text report renders", "cop_edition:           3.1" in out["3.1"][0].as_text())
    pset4 = " ".join(f.message for f in out["4"][1]["PSET.001"].findings)
    check("undeclared subtype requires only sets common to every wall component",
          "SGPset_Wall" in pset4 and "Pset_WallCommon" not in pset4
          and "SGPset_WallReinforcement" not in pset4, pset4[:160])
    every = all(f.all_ids for r in out["4"][1].values() for f in r.findings)
    check("every finding points to at least one object", every)


def requirement_semantics():
    print("Requirements follow the identified component")
    new = Library.discover(ROOT, cop="4")
    old = Library.discover(ROOT, cop="3.1")
    sets, basis = new.required_psets("IFCWALL", "*BOUNDARYWALL")
    check("declared subtype selects its component plus its general rows",
          {"SGPset_Material", "SGPset_Wall", "SGPset_WallDimension"} <= set(sets)
          and "Wall" in basis, str(sets))
    check("COP editions differ where the workbooks differ (IsExternal moved)",
          "Pset_WallCommon" in sets
          and "Pset_WallCommon" not in old.required_psets("IFCWALL", "*BOUNDARYWALL")[0])
    check("standard-case entities use their supertype mapping",
          new.required_psets("IFCWALLSTANDARDCASE", None)
          == new.required_psets("IFCWALL", None))
    check("entity with no identified component is not required to carry SGPsets",
          new.required_psets("IFCMEMBER", None) is None)
    check("a component's rows combine: lightning tape still needs its set",
          "SGPset_BuildingElementProxy"
          in new.required_psets("IFCBUILDINGELEMENTPROXY", "*TAPE")[0],
          str(new.required_psets("IFCBUILDINGELEMENTPROXY", "*TAPE")))
    stair = new.required_psets("IFCSTAIR", "STRAIGHT_RUN_STAIR")[0]
    check("a declared subtype keeps the component's general sets",
          "SGPset_Stair" in stair and "Pset_StairCommon" in stair, str(stair))
    check("PARAPET keeps SGPset_WallDimension",
          "SGPset_WallDimension" in new.required_psets("IFCWALL", "PARAPET")[0])
    empty = []
    for lib in (new, old):
        for item in lib.identified_components:
            for variant in item.get("variants") or []:
                if not variant.get("property_sets"):
                    continue
                for token in variant.get("subtypes") or []:
                    if (item["entity"].upper() == "IFCSPACE"
                            and lib.area_scheme_by_token(token) is not None):
                        continue  # area tokens resolve through the area-scheme table
                    sets, _basis = lib.required_psets(item["entity"], token)
                    if not sets:
                        empty.append("%s %s %s" % (lib.editions["cop_edition"],
                                                   item["entity"], token))
    check("no mapped component/subtype requires nothing", not empty, ", ".join(empty[:8]))
    space = new.required_psets("IFCSPACE", None)[0]
    check("undeclared IfcSpace is not asked for every area scheme",
          "SGPset_SpaceArea_Strata" not in space and "SGPset_SpaceArea_GFA" not in space,
          str(space))
    check("COP 4 PDF reconciliation recorded",
          new.metadata.get("cop_pdf_reconciliation", {}).get("pdf_property_pairs") == 601)
    check("COP 3.1 PDF reconciliation recorded",
          old.metadata.get("cop_pdf_reconciliation", {}).get("pdf_property_pairs") == 436)
    check("subtype with property set N.A is still a valid token",
          "DROPINLETCHAMBER" in new.valid_subtypes("IFCBUILDINGELEMENTPROXY"))
    check("subtype listed only in the COP document is valid",
          "RINSESHOWER" in new.valid_subtypes("IFCSANITARYTERMINAL")
          and "RINSESHOWER" not in old.valid_subtypes("IFCSANITARYTERMINAL"))
    check("PDF datatype accepted alongside workbook datatype",
          new.property_datatypes("SGPset_Site", "CXBlockID") == ["Label", "Integer"])
    check("PDF spelling accepted", "BeamFacade" in _props(new, "SGPset_Wall")
          and "BeamFa\u00e7ade" in _props(new, "SGPset_Wall"))

    with open(FIXTURE, "r", encoding="utf-8") as handle:
        text = handle.read()
    proxy = ("#99= IFCBUILDINGELEMENTPROXY('1Fixture0Proxy00000001',#5,'Rod',$,"
             "'*INSULATEDCABLE',#14,$,$,.USERDEFINED.);\n"
             "#97= IFCBUILDINGELEMENTPROXY('1Fixture0Proxy00000003',#5,'Inlet',$,"
             "'*DROPINLETCHAMBER',#14,$,$,.USERDEFINED.);\n"
             "#98= IFCBUILDINGELEMENTPROXY('1Fixture0Proxy00000002',#5,'Thing',$,"
             "'Generic Models',#14,$,$,.USERDEFINED.);\nENDSEC;")
    with tempfile.TemporaryDirectory(prefix="ifcsg-sem-") as folder:
        ifc4 = os.path.join(folder, "proxy.ifc")
        with open(ifc4, "w", encoding="utf-8") as handle:
            handle.write(text.replace("ENDSEC;\nEND-ISO", proxy + "\nEND-ISO", 1)
                         if "ENDSEC;\nEND-ISO" in text else _append_proxy(text, proxy))
        _ctx, results = Engine(new).run(IfcFile.read(ifc4))
        class1 = dict((r.rule_id, r) for r in results)["CLASS.001"]
        names = [f.name for f in class1.findings]
        check("mapped proxy component accepted, unmapped proxy flagged",
              names == ["Thing"], str(names))

        legacy = os.path.join(folder, "legacy.ifc")
        with open(legacy, "w", encoding="utf-8") as handle:
            handle.write(text.replace("FILE_SCHEMA(('IFC4'))", "FILE_SCHEMA(('IFC2X3'))"))
        _ctx, results = Engine(new).run(IfcFile.read(legacy))
        by_id = dict((r.rule_id, r) for r in results)
        check("IFC2X3 subtype rules are not applicable instead of misread",
              by_id["CLASS.002"].status == "NOT_APPLICABLE"
              and by_id["CLASS.003"].status == "NOT_APPLICABLE"
              and by_id["SCHEMA.001"].status == "FAIL")


def _append_proxy(text, proxy):
    head, _sep, tail = text.rpartition("ENDSEC;")
    return head + proxy + tail


def edition_rebuild():
    print("New edition rebuild removes withdrawn properties")
    with tempfile.TemporaryDirectory(prefix="ifcsg-rebuild-") as folder:
        root = os.path.join(folder, "root")
        shutil.copytree(os.path.join(ROOT, "data"), os.path.join(root, "data"))
        previous = os.path.join(folder, "previous.xlsx")
        current = os.path.join(folder, "current.xlsx")
        _write_xlsx(previous)
        _write_xlsx(current)
        # The previous workbook also published FireRating on SGPset_Test; the
        # current one renames the set, so FireRating must leave SGPset_Test.
        _rename_set(current, "SGPset_Test", "SGPset_Test2")
        run = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "build_catalogue.py"),
             previous, "--root", root, "--cop-edition", "4", "--mapping-edition",
             "2026-01-01"],
            capture_output=True, text=True)
        check("seed workbook imports into existing edition", run.returncode == 0,
              run.stderr[-300:])
        run = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "build_catalogue.py"),
             current, "--root", root, "--cop-edition", "5", "--from", "4",
             "--mapping-edition", "2027-01-01", "--previous-mapping", previous,
             "--cop-published", "2027-01"],
            capture_output=True, text=True)
        check("--from creates a new edition", run.returncode == 0, run.stderr[-300:])
        new = Library.discover(root)
        check("new edition becomes the default", new.editions["cop_edition"] == "5")
        check("withdrawn property removed from the new edition",
              "SGPset_Test" not in new.sgpsets
              and "FireRating" in _props(new, "SGPset_Test2"))
        check("removal is recorded",
              "SGPset_Test.FireRating" in new.metadata.get("removed_properties", []))
        old = Library.discover(root, cop="4")
        check("previous edition keeps its definition",
              "FireRating" in _props(old, "SGPset_Test"))
        check("previous edition now superseded", old.is_superseded)
        run = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "run_ifcsg_checker.py"),
             "--library", root, "--list-cops"], capture_output=True, text=True)
        check("launcher lists every edition",
              [line.split()[0] for line in run.stdout.splitlines()] == ["5", "4", "3.1"],
              run.stdout.strip().replace("\n", " / "))


def _rename_set(path, old, new):
    import zipfile
    with zipfile.ZipFile(path) as zf:
        items = dict((name, zf.read(name)) for name in zf.namelist())
    key = "xl/worksheets/sheet1.xml"
    items[key] = items[key].replace(old.encode(), new.encode())
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in items.items():
            zf.writestr(name, data)


def offline():
    print("Runs offline")
    package = os.path.join(TOOLS, "ifcsg_checker")
    banned = ("urllib", "http.client", "requests", "socket", "ftplib", "ssl")
    hits = []
    for name in sorted(os.listdir(package)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(package, name), "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")) and any(
                        b in stripped.split("#")[0] for b in banned):
                    hits.append("%s: %s" % (name, stripped))
    check("checker imports no network module", not hits, "; ".join(hits))
    catalogues = os.listdir(os.path.join(ROOT, "data", "catalogues"))
    check("every COP catalogue ships as a local file",
          sorted(catalogues) == ["cop-3.1.json", "cop-4.json"], str(catalogues))


def main():
    offline()
    editions()
    cop4_content()
    checks_follow_edition()
    requirement_semantics()
    edition_rebuild()
    print("\nFAILURES: %d" % len(FAILURES))
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
