"""Crash-resistance tests for streaming IFC reads and long-running UI work."""

import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ifcsg_checker import spf  # noqa: E402


FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


def main():
    original_chunk = spf.READ_CHUNK
    with tempfile.TemporaryDirectory(prefix="ifcsg-stability-") as folder:
        source = os.path.join(folder, "stream.ifc")
        payload = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('ViewDefinition [ReferenceView_V1.2]'),'2;1');
FILE_NAME('stream.ifc','2026-09-04T00:00:00',(),(),'test','test','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
/* a comment deliberately crossing tiny read chunks */
#1=IFCPROJECT('guid',$,'A; project',$,$,$,$,$,$);
#2=IFCWALL('wallguid',$,'Wall ''A''',$,$,$,$,$,.SOLIDWALL.);
#3=IFCCARTESIANPOINT((0.,0.,0.));
ENDSEC;
END-ISO-10303-21;
"""
        with open(source, "w", encoding="utf-8", newline="") as fh:
            fh.write(payload)

        print("Streaming parser")
        spf.READ_CHUNK = 7
        parsed = spf.IfcFile.read(source)
        check("tiny chunks count all entities", parsed.count() == 3,
              str(parsed.count()))
        check("lean mode drops heavy geometry records",
              parsed.retained_count() == 2
              and parsed.type_counts().get("IFCCARTESIANPOINT") == 1,
              "%d retained" % parsed.retained_count())
        check("semicolon inside a string is preserved",
              parsed.get(1).value(2) == "A; project", parsed.get(1).value(2))
        check("escaped quote across chunks is preserved",
              parsed.get(2).value(2) == "Wall 'A'", parsed.get(2).value(2))
        check("header metadata survives streaming", parsed.schema == "IFC4",
              str(parsed.schema))

        print("IFCZIP")
        archive = os.path.join(folder, "stream.ifczip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("nested/model.ifc", payload)
        zipped = spf.IfcFile.read(archive)
        check("IFCZIP selects and parses its IFC member", zipped.count() == 3,
              str(zipped.count()))

        print("Cancellation and format errors")
        calls = [0]

        def cancel():
            calls[0] += 1
            return calls[0] > 3

        try:
            spf.IfcFile.read(source, cancel=cancel)
        except spf.OperationCancelled:
            cancelled = True
        else:
            cancelled = False
        check("stream parsing honours cancellation", cancelled)

        xml_path = os.path.join(folder, "model.ifcxml")
        with open(xml_path, "w", encoding="utf-8") as fh:
            fh.write("<ifcXML/>")
        try:
            spf.IfcFile.read(xml_path)
        except spf.UnsupportedIfcFormat:
            rejected = True
        else:
            rejected = False
        check("IFCXML is rejected explicitly instead of misparsed", rejected)

    spf.READ_CHUNK = original_chunk
    print("\nFAILURES: %d" % len(FAILURES))
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
