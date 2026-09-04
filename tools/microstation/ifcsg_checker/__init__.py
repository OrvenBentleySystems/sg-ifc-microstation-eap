"""IFC+SG / CORENET X pre-flight checker for MicroStation.

Deterministic: the same IFC file and the same library edition always produce the
same findings, in the same order. Nothing resolves by guessing; an unresolvable
check is reported UNKNOWN, never PASS.
"""

__version__ = "1.3.0"

TOOL_NAME = "IFC+SG Checker - CORENET X Pre-flight"

__all__ = ["spf", "library", "importer", "rules", "sources",
           "locate", "report", "ui"]
