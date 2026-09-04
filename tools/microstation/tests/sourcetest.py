"""Reference-path resolution tests using MicroStation-shaped fakes."""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ifcsg_checker import sources  # noqa: E402


class FakeDgnFile(object):
    def __init__(self, path):
        self.path = path

    def GetFileName(self):
        return self.path


class FakeAttachment(object):
    def __init__(self, stored, loaded):
        self.stored = stored
        self.loaded = loaded

    def GetAttachFileName(self):
        return self.stored

    def GetDgnFile(self):
        return FakeDgnFile(self.loaded)


def main():
    failures = []

    def check(label, ok, detail=""):
        print(("  PASS  " if ok else "  FAIL  ") + label
              + ("   " + detail if detail else ""))
        if not ok:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="ifcsg-sources-") as folder:
        active_dir = os.path.join(folder, "workset")
        source_dir = os.path.join(folder, "downloads")
        os.makedirs(active_dir)
        os.makedirs(source_dir)
        active = os.path.join(active_dir, "master.dgn")
        loaded = os.path.join(source_dir, "referenced-model.ifc")
        open(active, "wb").close()
        open(loaded, "wb").close()

        att = FakeAttachment("referenced-model.ifc", loaded)
        resolved = sources._attachment_path(att, active)
        check("loaded absolute path wins over attachment basename",
              resolved == loaded, str(resolved))

        local = os.path.join(active_dir, "local.ifc")
        open(local, "wb").close()
        att = FakeAttachment("local.ifc", "")
        resolved = sources._attachment_path(att, active)
        check("relative attachment resolves beside active DGN",
              resolved == local, str(resolved))

        source = sources.Source(
            sources.KIND_IFC_REFERENCE, "Reference: old.ifc",
            path="old.ifc", attachment=FakeAttachment("old.ifc", loaded))
        check("live source refresh updates stale path",
              sources.resolve_source_path(source, active) == loaded
              and source.path == loaded, source.path)

    print("\nFAILURES: %d" % len(failures))
    for failure in failures:
        print("  - " + failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
