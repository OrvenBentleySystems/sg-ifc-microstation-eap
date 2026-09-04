"""Discovery of checkable IFC sources in the running MicroStation session.

Two source kinds are supported:

    IFC file   an .ifc opened natively or attached as a reference. The file itself is
               parsed, so every IFC+SG rule can be evaluated.
    DGN model  a native DGN model. Base MicroStation strips IFC semantics from an
               attached .ifc on import, so a DGN model is reported honestly for what
               it carries rather than having an IFC entity guessed for it.
"""

import os

IFC_EXTENSIONS = (".ifc", ".ifczip")
MAX_DGN_INSPECTION_ELEMENTS = 25000

KIND_IFC_ACTIVE = "IFC_ACTIVE"
KIND_IFC_REFERENCE = "IFC_REFERENCE"
KIND_IFC_BROWSED = "IFC_BROWSED"
KIND_DGN_ACTIVE = "DGN_ACTIVE"
KIND_DGN_REFERENCE = "DGN_REFERENCE"

IMPORT_ERROR = ""
LAST_DISCOVERY_ERROR = ""
ISessionMgr = None
IN_MICROSTATION = False
try:
    # ISessionMgr is exposed by MSPyMstnPlatform, not MSPyDgnPlatform.
    from MSPyMstnPlatform import ISessionMgr
    IN_MICROSTATION = True
except Exception as _exc:
    IMPORT_ERROR = "%s: %s" % (type(_exc).__name__, _exc)
    try:
        from MSPyDgnPlatform import ISessionMgr
        IN_MICROSTATION = True
        IMPORT_ERROR = ""
    except Exception as _exc2:
        IMPORT_ERROR += " | MSPyDgnPlatform: %s" % _exc2


class Source(object):
    __slots__ = ("kind", "label", "path", "attachment", "model_ref", "note")

    def __init__(self, kind, label, path=None, attachment=None, model_ref=None, note=""):
        self.kind = kind
        self.label = label
        self.path = path
        self.attachment = attachment
        self.model_ref = model_ref
        self.note = note

    @property
    def is_ifc(self):
        return self.kind in (KIND_IFC_ACTIVE, KIND_IFC_REFERENCE, KIND_IFC_BROWSED)

    @property
    def is_locatable(self):
        """True when findings can be tied back to geometry in the session."""
        return self.attachment is not None or self.kind == KIND_IFC_ACTIVE

    def __str__(self):
        return self.label


def _is_ifc(path):
    return bool(path) and os.path.splitext(path)[1].lower() in IFC_EXTENSIONS


def _file_name_of(dgn_file):
    try:
        return str(dgn_file.GetFileName())
    except Exception:
        return None


def _attachment_path(attachment, active_path=None):
    """Resolve an attachment name to the real file MicroStation has open.

    GetAttachFileName may return only the stored relative/basename value. The
    loaded DgnFile normally carries the fully resolved path and must take
    precedence when it exists.
    """
    stored = None
    loaded = None
    try:
        stored = str(attachment.GetAttachFileName() or "").strip()
    except Exception:
        pass
    try:
        loaded = _file_name_of(attachment.GetDgnFile())
    except Exception:
        pass

    candidates = []
    for value in (loaded, stored):
        if value and os.path.isabs(value):
            candidates.append(os.path.normpath(value))
    if active_path:
        folder = os.path.dirname(active_path)
        for value in (stored, loaded):
            if value and not os.path.isabs(value):
                candidates.append(os.path.normpath(os.path.join(folder, value)))
    for value in (loaded, stored):
        if value:
            candidates.append(os.path.normpath(value))

    seen = set()
    for candidate in candidates:
        norm = os.path.normcase(os.path.abspath(candidate))
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isfile(candidate):
            return candidate
    return candidates[0] if candidates else None


def resolve_source_path(source, active_path=None):
    """Refresh a reference source path from its live attachment."""
    if source.attachment is None:
        return source.path
    path = _attachment_path(source.attachment, active_path=active_path)
    if path:
        source.path = path
    return source.path


def discover():
    """Return every checkable source in the active session, active file first."""
    globals()["LAST_DISCOVERY_ERROR"] = ""
    if not IN_MICROSTATION:
        return []

    sources = []
    try:
        model_ref = ISessionMgr.GetActiveDgnModelRef()
    except Exception as exc:
        globals()["LAST_DISCOVERY_ERROR"] = "Cannot access the active model: %s" % exc
        return []
    if model_ref is None:
        globals()["LAST_DISCOVERY_ERROR"] = "MicroStation returned no active model."
        return []

    active_path = None
    try:
        active_path = _file_name_of(ISessionMgr.GetActiveDgnFile())
    except Exception:
        pass

    if active_path:
        base = os.path.basename(active_path)
        if _is_ifc(active_path):
            sources.append(Source(
                KIND_IFC_ACTIVE, "Active file: %s" % base, path=active_path,
                model_ref=model_ref))
        else:
            sources.append(Source(
                KIND_DGN_ACTIVE, "Active DGN model: %s" % base, path=active_path,
                model_ref=model_ref,
                note="Native DGN. IFC+SG data must come from DataGroup/Item Type "
                     "properties; geometry alone cannot be rule-checked."))

    try:
        attachments = model_ref.GetDgnAttachments()
    except Exception:
        attachments = None

    if attachments:
        seen_paths = set()
        for att in attachments:
            path = _attachment_path(att, active_path=active_path)
            if not path:
                continue
            norm = os.path.normcase(os.path.abspath(path))
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            base = os.path.basename(path)
            logical = ""
            try:
                logical = str(att.GetLogicalName() or "")
            except Exception:
                pass
            tag = " [%s]" % logical if logical else ""
            if _is_ifc(path):
                sources.append(Source(
                    KIND_IFC_REFERENCE, "Reference: %s%s" % (base, tag),
                    path=path, attachment=att))
            else:
                sources.append(Source(
                    KIND_DGN_REFERENCE, "Reference (DGN): %s%s" % (base, tag),
                    path=path, attachment=att,
                    note="DGN reference. IFC+SG data must come from DataGroup/Item "
                         "Type properties."))
    return sources


def browsed_source(path):
    return Source(KIND_IFC_BROWSED, "File: %s" % os.path.basename(path), path=path)


# ---------------------------------------------------------------------------
# Native DGN inspection
# ---------------------------------------------------------------------------

def inspect_dgn(source, max_elements=MAX_DGN_INSPECTION_ELEMENTS):
    """Report what IFC+SG-relevant data a native DGN model actually carries.

    Deliberately makes no attempt to infer an IFC entity from a level name or a
    cell name. An inferred classification would pass local review and fail at
    submission, which is the failure mode this tool exists to prevent.
    """
    result = {
        "elements": 0,
        "with_ec": 0,
        "with_items": 0,
        "ec_classes": {},
        "item_classes": {},
        "errors": [],
        "truncated": False,
    }
    if not IN_MICROSTATION:
        result["errors"].append("Not running inside MicroStation.")
        return result

    from MSPyDgnPlatform import (DgnECManager, FindInstancesScope, FindInstancesScopeOption,
                                 ElementHandle, CustomItemHost)
    from MSPyECObjects import DgnECHostType, ECQuery, ECQueryProcessFlags, DgnECInstanceVector

    model_ref = source.attachment if source.attachment is not None else source.model_ref
    try:
        model = model_ref.GetDgnModel()
    except Exception as exc:
        result["errors"].append("Cannot open model: %s" % exc)
        return result
    if model is None:
        result["errors"].append("MicroStation returned no DGN model.")
        return result

    mgr = DgnECManager.GetManager()
    if mgr is None:
        result["errors"].append("MicroStation EC manager is unavailable.")
        return result
    opts = FindInstancesScopeOption(DgnECHostType.eElement)
    flags = ECQueryProcessFlags.eECQUERY_PROCESS_SearchAllClasses

    for ref in model.GetGraphicElements():
        if result["elements"] >= max_elements:
            result["truncated"] = True
            break
        result["elements"] += 1
        eh = ElementHandle(ref, model)
        try:
            vec = DgnECInstanceVector()
            if CustomItemHost(eh).GetCustomItems(vec):
                result["with_items"] += 1
                for inst in vec:
                    key = _class_key(inst)
                    result["item_classes"][key] = result["item_classes"].get(key, 0) + 1
        except Exception:
            pass
        try:
            found = mgr.FindInstances(FindInstancesScope.CreateScope(eh, opts),
                                      ECQuery.CreateQuery(flags))
            iterable = found[0] if isinstance(found, tuple) else found
            hit = False
            for inst in iterable:
                key = _class_key(inst)
                if key.startswith("DgnElementSchema:"):
                    continue          # intrinsic geometry class, not business data
                hit = True
                result["ec_classes"][key] = result["ec_classes"].get(key, 0) + 1
            if hit:
                result["with_ec"] += 1
        except Exception as exc:
            if len(result["errors"]) < 3:
                result["errors"].append(str(exc))
    return result


def _class_key(instance):
    try:
        cls = instance.GetClass()
        return "%s:%s" % (cls.GetSchema().GetName(), cls.GetName())
    except Exception:
        return "?"
