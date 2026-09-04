"""Performance and export harness for production-sized IFC models.

Usage:
    python realmodeltest.py --ifc "model.ifc" --exports --expect-walls 1126
"""

import argparse
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(TOOLS))
sys.path.insert(0, TOOLS)

from ifcsg_checker import bcf as bcf_mod  # noqa: E402
from ifcsg_checker import report as report_mod  # noqa: E402
from ifcsg_checker import sources as sources_mod  # noqa: E402
from ifcsg_checker.library import Library  # noqa: E402
from ifcsg_checker.rules import Engine  # noqa: E402
from ifcsg_checker.spf import IfcFile  # noqa: E402


def peak_working_set_mb():
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    get_memory = kernel32.K32GetProcessMemoryInfo
    get_memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    get_memory.restype = wintypes.BOOL
    ok = get_memory(
        process, ctypes.byref(counters), counters.cb)
    return counters.PeakWorkingSetSize / (1024.0 * 1024.0) if ok else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ifc", required=True)
    parser.add_argument("--library", default=ROOT)
    parser.add_argument("--exports", action="store_true")
    parser.add_argument("--expect-walls", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-peak-mb", type=float)
    args = parser.parse_args()

    failures = []
    start = time.perf_counter()
    ifc = IfcFile.read(args.ifc)
    parsed_at = time.perf_counter()
    library = Library.discover(args.library)
    ctx, results = Engine(library).run(ifc)
    checked_at = time.perf_counter()
    report = report_mod.Report(
        sources_mod.browsed_source(args.ifc), ifc, ctx, results, library)

    walls = len(ifc.of_types(("IFCWALL", "IFCWALLSTANDARDCASE",
                              "IFCWALLELEMENTEDCASE")))
    findings = sum(len(result.findings) for result in results)
    total_seconds = checked_at - start
    peak_mb = peak_working_set_mb()

    print("entities:       %d" % ifc.count())
    print("retained:       %d" % ifc.retained_count())
    print("model elements: %d" % len(ctx.element_ids))
    print("walls:          %d" % walls)
    print("findings:       %d" % findings)
    print("parse seconds:  %.2f" % (parsed_at - start))
    print("rule seconds:   %.2f" % (checked_at - parsed_at))
    print("total seconds:  %.2f" % total_seconds)
    if peak_mb is not None:
        print("peak memory MB: %.1f" % peak_mb)

    if args.exports:
        with tempfile.TemporaryDirectory(prefix="ifcsg-realmodel-") as folder:
            report.write_text(os.path.join(folder, "report.txt"))
            report.write_csv(os.path.join(folder, "report.csv"))
            report.write_json(os.path.join(folder, "report.json"))
            report.write_html(os.path.join(folder, "report.html"))
            _path, topics = bcf_mod.write_bcf(
                report, os.path.join(folder, "report.bcfzip"))
            written = sorted(os.listdir(folder))
            if len(written) != 5 or topics < 1:
                failures.append("all five exports must be produced")
            print("exports:        %s (%d BCF topics)" % (", ".join(written), topics))

    if args.expect_walls is not None and walls != args.expect_walls:
        failures.append("expected %d walls, got %d" % (args.expect_walls, walls))
    if args.max_seconds is not None and total_seconds > args.max_seconds:
        failures.append("runtime %.2fs exceeds %.2fs" % (total_seconds, args.max_seconds))
    if (args.max_peak_mb is not None and peak_mb is not None
            and peak_mb > args.max_peak_mb):
        failures.append("peak memory %.1f MB exceeds %.1f MB"
                        % (peak_mb, args.max_peak_mb))

    if failures:
        print("FAIL")
        for failure in failures:
            print("  - " + failure)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
