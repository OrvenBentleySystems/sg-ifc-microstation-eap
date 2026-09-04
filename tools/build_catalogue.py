#!/usr/bin/env python3
"""Update data/catalogue.json from one or more IFC+SG mapping files."""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MICROSTATION = os.path.join(ROOT, "tools", "microstation")
sys.path.insert(0, MICROSTATION)

from ifcsg_checker import importer  # noqa: E402
from ifcsg_checker.library import Library  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mapping", nargs="+",
        help="Official .xlsx mapping or supported .csv/.json/.md/.txt export")
    parser.add_argument("--root", default=ROOT, help="Repository or installed tool root")
    parser.add_argument("--mapping-edition", required=True, help="YYYY-MM-DD")
    parser.add_argument("--cop-edition", required=True, help="For example 3.1")
    parser.add_argument(
        "--replace-property-sets", action="store_true",
        help="Clear bundled property sets before importing the first mapping")
    args = parser.parse_args(argv)

    for index, path in enumerate(args.mapping):
        summary = importer.merge(
            args.root,
            path,
            mapping_edition=args.mapping_edition,
            cop_edition=args.cop_edition,
            replace=args.replace_property_sets and index == 0,
            log=print)
        print("Updated from %s: %d sets, %d properties"
              % (summary["file"], summary["sets_total"],
                 summary["properties_total"]))

    library = Library.discover(args.root)
    print("Catalogue: %s" % library.path)
    print("Mapping: %s | COP: %s | SGPsets: %d"
          % (library.editions["mapping_edition"],
             library.editions["cop_edition"],
             library.editions["sgpset_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
