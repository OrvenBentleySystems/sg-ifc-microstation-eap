#!/usr/bin/env python3
"""Create or update a COP edition catalogue from IFC+SG mapping files.

New COP edition (seeded from the previous one, withdrawn properties removed):

    python tools/build_catalogue.py industry-mapping-09-2026.xlsx \
        --cop-edition 4 --mapping-edition 2026-09-25 --from 3.1 \
        --previous-mapping industry-mapping-4-dec-2025.xlsx

Update an existing edition in place:

    python tools/build_catalogue.py mapping.xlsx --cop-edition 4 --mapping-edition 2026-10-01
"""

import argparse
import json
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MICROSTATION = os.path.join(ROOT, "tools", "microstation")
sys.path.insert(0, MICROSTATION)

from ifcsg_checker import importer  # noqa: E402
from ifcsg_checker.library import CATALOGUE_DIR, Library, same_cop  # noqa: E402


def catalogue_file(root, cop):
    safe = re.sub(r"[^0-9A-Za-z.]+", "-", str(cop)).strip("-")
    return os.path.join(root, "data", CATALOGUE_DIR, "cop-%s.json" % safe)


def _existing(root, cop):
    try:
        editions = Library.available(root)
    except IOError:
        return None
    for item in editions:
        if same_cop(item.cop_edition, cop):
            return item.path
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "mapping", nargs="+",
        help="Official .xlsx mapping or supported .csv/.json/.md/.txt export")
    parser.add_argument("--root", default=ROOT, help="Repository or installed tool root")
    parser.add_argument("--mapping-edition", required=True, help="YYYY-MM-DD")
    parser.add_argument("--cop-edition", required=True, help="For example 4 or 3.1")
    parser.add_argument("--from", dest="base",
                        help="Seed a new edition from this installed COP edition")
    parser.add_argument("--previous-mapping",
                        help="Previous official workbook; withdrawn properties are removed")
    parser.add_argument("--cop-published", help="Publication month, YYYY-MM")
    parser.add_argument("--cop-title", help="For example 'CORENET X COP 4th Edition'")
    parser.add_argument("--cop-url", help="Official COP PDF URL")
    parser.add_argument("--cop-changes-url", help="Official summary of changes URL")
    parser.add_argument(
        "--replace-property-sets", action="store_true",
        help="Clear property sets before importing the first mapping")
    args = parser.parse_args(argv)

    target = _existing(args.root, args.cop_edition)
    if args.base:
        if target:
            parser.error("COP %s already exists (%s); drop --from to update it."
                         % (args.cop_edition, target))
        source = _existing(args.root, args.base)
        if not source:
            parser.error("Base COP edition %s is not installed." % args.base)
        target = catalogue_file(args.root, args.cop_edition)
        shutil.copyfile(source, target)
        print("Seeded %s from %s" % (target, source))
    elif not target:
        parser.error("COP %s is not installed. Use --from to seed it from an existing "
                     "edition." % args.cop_edition)

    for index, path in enumerate(args.mapping):
        summary = importer.merge(
            args.root,
            path,
            mapping_edition=args.mapping_edition,
            cop_edition=args.cop_edition,
            replace=args.replace_property_sets and index == 0,
            log=print,
            catalogue_path=target,
            previous=args.previous_mapping if index == 0 else None)
        print("Updated from %s: %d sets, %d properties, %d removed"
              % (summary["file"], summary["sets_total"],
                 summary["properties_total"], summary["properties_removed"]))

    with open(target, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    metadata = payload["metadata"]
    for key, value in (("cop_published", args.cop_published),
                       ("cop_title", args.cop_title),
                       ("cop_url", args.cop_url),
                       ("cop_changes_url", args.cop_changes_url)):
        if value:
            metadata[key] = value
        elif args.base:
            metadata.pop(key, None)
    metadata["verified_at"] = args.mapping_edition if args.base else metadata.get(
        "verified_at", args.mapping_edition)
    importer._atomic_json_dump(target, payload)

    library = Library.discover(args.root, cop=args.cop_edition)
    print("Catalogue: %s" % library.path)
    print("Mapping: %s | COP: %s | SGPsets: %d"
          % (library.editions["mapping_edition"],
             library.editions["cop_edition"],
             library.editions["sgpset_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
