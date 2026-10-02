"""Object pictures, the objects-to-fix list and the HTML report cards."""

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)

from ifcsg_checker import ifcgeom, preview  # noqa: E402
from ifcsg_checker import report as report_mod  # noqa: E402
from ifcsg_checker import sources as sources_mod  # noqa: E402
from ifcsg_checker.library import Library  # noqa: E402
from ifcsg_checker.locate import ifc_length_to_metres  # noqa: E402
from ifcsg_checker.rules import Engine  # noqa: E402
from ifcsg_checker.spf import IfcFile  # noqa: E402

FAILURES = []

# Millimetre units. A 5000 x 200 x 3000 wall extrusion placed at (1000,2000,0),
# a door built from a mapped, triangulated unit cube scaled 900 x 100 x 2100,
# and a slab as a faceted brep box 4000 x 3000 x 250.
GEOMETRY_IFC = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');
FILE_NAME('geom.ifc','2026-10-02T00:00:00',(''),(''),'','','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCSIUNIT(*,.LENGTHUNIT.,.MILLI.,.METRE.);
#2=IFCUNITASSIGNMENT((#1));
#3=IFCPROJECT('0Geom000Project00000001',$,'P',$,$,$,$,$,#2);
#4=IFCCARTESIANPOINT((0.,0.,0.));
#5=IFCAXIS2PLACEMENT3D(#4,$,$);
#6=IFCLOCALPLACEMENT($,#5);
#7=IFCSITE('0Geom000Site0000000001',$,'Site',$,$,#6,$,$,.ELEMENT.,$,$,$,$,$);
#8=IFCBUILDING('0Geom000Building000001',$,'B',$,$,#6,$,$,.ELEMENT.,$,$,$);
#9=IFCBUILDINGSTOREY('0Geom000Storey00000001',$,'L1',$,$,#6,$,$,.ELEMENT.,0.);
#10=IFCRELAGGREGATES('0Geom000Agg00000000001',$,$,$,#3,(#7));
#11=IFCRELAGGREGATES('0Geom000Agg00000000002',$,$,$,#7,(#8));
#12=IFCRELAGGREGATES('0Geom000Agg00000000003',$,$,$,#8,(#9));
#20=IFCCARTESIANPOINT((1000.,2000.,0.));
#21=IFCAXIS2PLACEMENT3D(#20,$,$);
#22=IFCLOCALPLACEMENT(#6,#21);
#23=IFCCARTESIANPOINT((2500.,100.));
#24=IFCAXIS2PLACEMENT2D(#23,$);
#25=IFCRECTANGLEPROFILEDEF(.AREA.,$,#24,5000.,200.);
#26=IFCDIRECTION((0.,0.,1.));
#27=IFCEXTRUDEDAREASOLID(#25,#5,#26,3000.);
#28=IFCSHAPEREPRESENTATION($,'Body','SweptSolid',(#27));
#29=IFCPRODUCTDEFINITIONSHAPE($,$,(#28));
#30=IFCWALL('0Geom000Wall0000000001',$,'Wall W1',$,$,#22,#29,$,.SOLIDWALL.);
#40=IFCCARTESIANPOINTLIST3D(((0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.),(0.,0.,1.),(1.,0.,1.),(1.,1.,1.),(0.,1.,1.)));
#41=IFCTRIANGULATEDFACESET(#40,$,.T.,((1,2,3),(1,3,4),(5,6,7),(5,7,8),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,4,8),(3,8,7),(4,1,5),(4,5,8)),$);
#42=IFCSHAPEREPRESENTATION($,'Body','Tessellation',(#41));
#43=IFCREPRESENTATIONMAP(#5,#42);
#44=IFCCARTESIANTRANSFORMATIONOPERATOR3DNONUNIFORM($,$,#4,900.,$,100.,2100.);
#45=IFCMAPPEDITEM(#43,#44);
#46=IFCSHAPEREPRESENTATION($,'Body','MappedRepresentation',(#45));
#47=IFCPRODUCTDEFINITIONSHAPE($,$,(#46));
#48=IFCDOOR('0Geom000Door0000000001',$,'Door D1',$,$,#22,#47,$,2100.,900.,.DOOR.,$,$);
#60=IFCCARTESIANPOINT((0.,0.,0.));
#61=IFCCARTESIANPOINT((4000.,0.,0.));
#62=IFCCARTESIANPOINT((4000.,3000.,0.));
#63=IFCCARTESIANPOINT((0.,3000.,0.));
#64=IFCCARTESIANPOINT((0.,0.,250.));
#65=IFCCARTESIANPOINT((4000.,0.,250.));
#66=IFCCARTESIANPOINT((4000.,3000.,250.));
#67=IFCCARTESIANPOINT((0.,3000.,250.));
#68=IFCPOLYLOOP((#60,#61,#62,#63));
#69=IFCPOLYLOOP((#64,#65,#66,#67));
#70=IFCFACEOUTERBOUND(#68,.T.);
#71=IFCFACEOUTERBOUND(#69,.T.);
#72=IFCFACE((#70));
#73=IFCFACE((#71));
#74=IFCCLOSEDSHELL((#72,#73));
#75=IFCFACETEDBREP(#74);
#76=IFCSHAPEREPRESENTATION($,'Body','Brep',(#75));
#77=IFCPRODUCTDEFINITIONSHAPE($,$,(#76));
#78=IFCSLAB('0Geom000Slab0000000001',$,'Slab S1',$,$,#6,#77,$,.FLOOR.);
#90=IFCRELCONTAINEDINSPATIALSTRUCTURE('0Geom000Contain0000001',$,$,$,(#30,#48,#78),#9);
ENDSEC;
END-ISO-10303-21;
"""


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def _size(wire):
    lo, hi = preview.bounds(wire)
    return tuple(round(hi[i] - lo[i], 3) for i in range(3))


def projection():
    print("Projection")
    cube = [[(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 0)],
            [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1), (0, 0, 1)]]
    lines = preview.project(cube, 200, 150)
    inside = all(0 <= v <= 200 for s in lines for v in (s[0], s[2])) and \
        all(0 <= v <= 150 for s in lines for v in (s[1], s[3]))
    check("projected edges fit the picture", bool(lines) and inside)
    check("far edges are drawn first", lines[0][4] >= lines[-1][4])
    check("empty wire gives no SVG", preview.svg([], 100, 100) is None)
    pic = preview.svg(cube, 240, 170, "cube")
    check("SVG has one line per edge", pic.count("<line") == 8, str(pic.count("<line")))
    check("segment cap is honoured",
          len(preview.segments([[(i, 0, 0) for i in range(5000)]], 100)) == 100)


def geometry(path):
    print("IFC geometry without MicroStation")
    f = IfcFile.read(path, retain_geometry=True)
    scale = ifc_length_to_metres(f)
    check("millimetre file scales to metres", abs(scale - 0.001) < 1e-12, str(scale))
    wall = ifcgeom.wireframe(f, f.of_type("IFCWALL")[0], unit_scale=scale)
    check("extruded wall has its real size", _size(wall) == (5.0, 0.2, 3.0), str(_size(wall)))
    lo, _hi = preview.bounds(wall)
    check("wall placement is applied", (round(lo[0], 3), round(lo[1], 3)) == (1.0, 2.0),
          str(lo))
    door = ifcgeom.wireframe(f, f.of_type("IFCDOOR")[0], unit_scale=scale)
    check("mapped, scaled triangulated door has its real size",
          _size(door) == (0.9, 0.1, 2.1), str(_size(door)))
    slab = ifcgeom.wireframe(f, f.of_type("IFCSLAB")[0], unit_scale=scale)
    check("faceted brep slab has its real size", _size(slab) == (4.0, 3.0, 0.25),
          str(_size(slab)))
    lean = IfcFile.read(path)
    check("lean parse draws nothing instead of guessing",
          ifcgeom.wireframe(lean, lean.of_type("IFCWALL")[0], unit_scale=scale) == [])

    extra = GEOMETRY_IFC.replace("ENDSEC;\nEND-ISO", """#200=IFCDIRECTION((-1.,0.,0.));
#201=IFCDIRECTION((0.,1.,0.));
#202=IFCDIRECTION((0.,0.,1.));
#203=IFCCARTESIANTRANSFORMATIONOPERATOR3D(#200,#201,#4,1.,#202);
#204=IFCCARTESIANPOINT((1.,2.,0.));
#210=IFCCARTESIANTRANSFORMATIONOPERATOR2DNONUNIFORM($,$,#4,1.,-1.);
#220=IFCLSHAPEPROFILEDEF(.AREA.,$,$,200.,100.,10.,$,$,$);
#230=IFCCIRCLE(#24,1000.);
#231=IFCTRIMMEDCURVE(#230,(IFCPARAMETERVALUE(0.)),(IFCPARAMETERVALUE(90.)),.T.,.PARAMETER.);
#232=IFCSIUNIT(*,.PLANEANGLEUNIT.,$,.RADIAN.);
#233=IFCMEASUREWITHUNIT(IFCPLANEANGLEMEASURE(0.017453292519943295),#232);
#234=IFCDIMENSIONALEXPONENTS(0,0,0,0,0,0,0);
#235=IFCCONVERSIONBASEDUNIT(#234,.PLANEANGLEUNIT.,'DEGREE',#233);
ENDSEC;
END-ISO""", 1)
    tmp = path + ".extra.ifc"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(extra)
    g = IfcFile.read(tmp, retain_geometry=True)
    from ifcsg_checker.spf import mat_apply
    mirrored = mat_apply(ifcgeom._operator_matrix(g, 203), (1.0, 2.0, 0.0))
    check("mirrored mapped item stays mirrored",
          tuple(round(v, 6) for v in mirrored) == (-1.0, 2.0, 0.0), str(mirrored))
    flipped = mat_apply(ifcgeom._operator_matrix(g, 210), (1.0, 2.0, 0.0))
    check("2D non-uniform operator applies Scale2 to Y",
          tuple(round(v, 6) for v in flipped) == (1.0, -2.0, 0.0), str(flipped))
    edges = ifcgeom._Edges(g)
    loop = edges.profile(220)[0]
    xs = [p[0] for p in loop]
    ys = [p[1] for p in loop]
    check("L-shape profile has depth along Y",
          (max(xs) - min(xs), max(ys) - min(ys)) == (100.0, 200.0),
          str((max(xs) - min(xs), max(ys) - min(ys))))
    arc = edges.curve_points(231)
    end = arc[-1]
    check("trimmed circle is an arc, not a full circle",
          abs(arc[0][0] - 3500.0) < 1e-6 and abs(end[0] - 2500.0) < 1e-6
          and abs(end[1] - 1100.0) < 1e-6 and len(arc) < 15, str((arc[0], end, len(arc))))


def objects_and_report(path, folder):
    print("Objects to fix and report cards")
    f = IfcFile.read(path, retain_geometry=True)
    lib = Library.discover(ROOT, cop="4")
    ctx, results = Engine(lib).run(f)
    rep = report_mod.Report(sources_mod.browsed_source(path), f, ctx, results, lib)
    rows = rep.failing_objects()
    check("every object to fix has at least one reason", rows and all(r["issues"] for r in rows))
    ranks = [report_mod.SEVERITY_RANK.get(r["severity"], 9) for r in rows]
    check("errors are listed before warnings", ranks == sorted(ranks))
    door = next(r for r in rows if r["entity"] == "IFCDOOR")
    check("an object row names the object and storey",
          door["name"] == "Door D1" and door["storey"] == "L1" and door["guid"])
    every = set()
    for r in rep.results:
        if r.status in ("FAIL", "WARN"):
            for fnd in r.findings:
                every.update(fnd.all_ids)
    check("every object named by a failing rule is listed",
          every == set(r["id"] for r in rows))
    pics = preview.collect_pictures(rep)
    check("every listed object gets a picture",
          set(pics) == set(r["id"] for r in rows if f.get(r["id"]).type
                           in ("IFCWALL", "IFCDOOR", "IFCSLAB")), str(sorted(pics)))
    html_path = os.path.join(folder, "r.html")
    rep.write_html(html_path, pictures=pics)
    with open(html_path, encoding="utf-8") as fh:
        html = fh.read()
    check("report has the objects-to-fix section", 'id="fix"' in html)
    check("each object card carries its picture",
          html.count('<article class="obj') == len(rows)
          and html.count("<svg") == len(pics))
    check("cards show the reason and the fix", "Fix:" in html and "PSET.001" in html)
    lines, note = preview.object_wire(rep, door["id"])
    check("preview draws an object without MicroStation", bool(lines) and not note)

    run = subprocess.run([sys.executable, os.path.join(TOOLS, "run_ifcsg_checker.py"),
                          "--ifc", path, "--out", os.path.join(folder, "cli.txt")],
                         capture_output=True, text=True)
    with open(os.path.join(folder, "cli.html"), encoding="utf-8") as fh:
        cli = fh.read()
    check("command-line report includes pictures",
          run.returncode == 0 and cli.count("<svg") == len(pics), run.stderr[-200:])


def main():
    with tempfile.TemporaryDirectory(prefix="ifcsg-preview-") as folder:
        path = os.path.join(folder, "geom.ifc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(GEOMETRY_IFC)
        projection()
        geometry(path)
        objects_and_report(path, folder)
    print("\nFAILURES: %d" % len(FAILURES))
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
