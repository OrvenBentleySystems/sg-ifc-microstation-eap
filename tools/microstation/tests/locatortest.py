"""Pure-Python tests for locator safety behavior."""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)

from ifcsg_checker import locate  # noqa: E402
from ifcsg_checker.spf import Entity  # noqa: E402


FAILURES = []


def check(label, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + label
          + ("   " + detail if detail else ""))
    if not ok:
        FAILURES.append(label)


class FakeIfc(object):
    geometry_available = False

    def __init__(self):
        self.entity = Entity(1, "IFCWALL", "'guid',$,'Wall',$,$,$,$,$,.SOLIDWALL.")

    def get(self, entity_id):
        return self.entity if entity_id == 1 else None


class NullSelectionManager(object):
    @staticmethod
    def GetManager():
        return None


def main():
    locator = locate.Locator.__new__(locate.Locator)
    locator.available = True
    locator.reason = ""
    locator.f = FakeIfc()
    locator._guids = {}
    locator.guid_hits = 0
    locator.geometry_hits = 0
    locator._model_ref = object()
    locator._model = object()
    locator.guid_index_truncated = False
    locator.index_truncated = False

    print("Lean locator")
    check("lean mode does not attempt spatial fallback",
          locator.matches(1) == [])

    original = getattr(locate, "SelectionSetManager", None)
    locate.SelectionSetManager = NullSelectionManager
    try:
        count, note = locator.select([1])
    finally:
        if original is None:
            delattr(locate, "SelectionSetManager")
        else:
            locate.SelectionSetManager = original
    check("missing selection manager is handled", count == 0, note)

    print("\nFAILURES: %d" % len(FAILURES))
    for failure in FAILURES:
        print("  - " + failure)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
