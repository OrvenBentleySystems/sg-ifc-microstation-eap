#!/usr/bin/env python3
"""Validate every bundled COP catalogue without third-party packages."""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "microstation"))

from ifcsg_checker.library import Library, cop_key  # noqa: E402


def validate(library):
    metadata = library.metadata
    errors = []
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata.get("mapping_edition", "")):
        errors.append("metadata.mapping_edition must be YYYY-MM-DD")
    if not metadata.get("cop_edition"):
        errors.append("metadata.cop_edition is required")
    published = metadata.get("cop_published")
    if published and not re.fullmatch(r"\d{4}-\d{2}", published):
        errors.append("metadata.cop_published must be YYYY-MM")
    for source in metadata.get("sources", []):
        digest = source.get("sha256", "")
        if digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            errors.append("invalid SHA-256 for %s" % source.get("name", "?"))
    return errors


def main():
    editions = Library.available(ROOT)
    if not editions:
        print("ERROR: no catalogues under data/catalogues")
        return 1
    failed = False
    seen = set()
    for edition in editions:
        key = cop_key(edition.cop_edition)
        name = os.path.basename(edition.path)
        if key in seen:
            print("ERROR: %s duplicates COP %s" % (name, edition.cop_edition))
            failed = True
            continue
        seen.add(key)
        try:
            library = Library(ROOT, edition.path)
        except (IOError, ValueError) as exc:
            print("ERROR: %s: %s" % (name, exc))
            failed = True
            continue
        errors = validate(library)
        for error in errors:
            print("ERROR: %s: %s" % (name, error))
        failed = failed or bool(errors)
        properties = sum(
            len(item.get("properties", []))
            for item in library.catalogue["property_sets"])
        print("%s  COP %s | mapping %s | %d sets | %d properties | %d components | %d rules"
              % (name, library.editions["cop_edition"],
                 library.editions["mapping_edition"],
                 len(library.catalogue["property_sets"]), properties,
                 len(library.catalogue["identified_components"]),
                 len(library.catalogue["rules"])))
    print("Catalogues invalid" if failed else "Catalogues valid (%d)" % len(editions))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
