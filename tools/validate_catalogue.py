#!/usr/bin/env python3
"""Validate the bundled IFC+SG catalogue without third-party packages."""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "microstation"))

from ifcsg_checker.library import Library  # noqa: E402


def main():
    library = Library.discover(ROOT)
    metadata = library.metadata
    errors = []
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata.get("mapping_edition", "")):
        errors.append("metadata.mapping_edition must be YYYY-MM-DD")
    if not metadata.get("cop_edition"):
        errors.append("metadata.cop_edition is required")
    for source in metadata.get("sources", []):
        digest = source.get("sha256", "")
        if digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            errors.append("invalid SHA-256 for %s" % source.get("name", "?"))
    if errors:
        for error in errors:
            print("ERROR: " + error)
        return 1
    properties = sum(
        len(item.get("properties", []))
        for item in library.catalogue["property_sets"])
    print("Catalogue valid")
    print("Mapping: %s" % library.editions["mapping_edition"])
    print("COP: %s" % library.editions["cop_edition"])
    print("Property sets: %d" % len(library.catalogue["property_sets"]))
    print("Properties: %d" % properties)
    print("Identified components: %d"
          % len(library.catalogue["identified_components"]))
    print("Rules: %d" % len(library.catalogue["rules"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
