"""Deterministic IFC+SG rule engine.

Every rule is a pure function of the parsed IFC file and the loaded library.
The same inputs always produce the same findings, in the same order.

A rule reports one of:
    PASS            the check was performed and satisfied
    FAIL            the check was performed and violated (severity from the library)
    WARN            the check was performed and raised a judgement item
    UNKNOWN         the check could not be performed with the loaded library
    NOT_APPLICABLE  the check does not apply to this input

UNKNOWN is never silently upgraded to PASS.
"""

import re

from . import spf
from .library import EXACT, CASE_MISMATCH, NEAR_MISS, UNKNOWN as RES_UNKNOWN, base_entity

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

STATUS_ORDER = {FAIL: 0, WARN: 1, UNKNOWN: 2, NOT_APPLICABLE: 3, PASS: 4}

GUID_ALPHABET = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_$")

PLACEHOLDER_VALUES = {
    "n.a.", "n/a", "na", "nil", "none", "null", "tbc", "tba", "tbd",
    "dwg number", "drawing number", "default", "-", "--", "xxx", "???",
    "enter value", "text", "value", "undefined", "unset",
}

SPATIAL_CLASSES = {"IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCSPACE"}
NON_ELEMENT_CLASSES = SPATIAL_CLASSES | {
    "IFCOPENINGELEMENT", "IFCGRID", "IFCGRIDAXIS", "IFCANNOTATION",
    "IFCVIRTUALELEMENT", "IFCSPATIALZONE", "IFCZONE", "IFCSYSTEM", "IFCGROUP",
}

DATATYPE_ACCEPTS = {
    "label": {"IFCLABEL", "IFCTEXT", "IFCIDENTIFIER"},
    "text": {"IFCTEXT", "IFCLABEL"},
    "identifier": {"IFCIDENTIFIER", "IFCLABEL", "IFCTEXT"},
    "boolean": {"IFCBOOLEAN", "IFCLOGICAL"},
    "logical": {"IFCLOGICAL", "IFCBOOLEAN"},
    "integer": {"IFCINTEGER", "IFCCOUNTMEASURE"},
    "count": {"IFCCOUNTMEASURE", "IFCINTEGER"},
    "real": {"IFCREAL", "IFCRATIOMEASURE", "IFCNORMALISEDRATIOMEASURE",
             "IFCPOSITIVERATIOMEASURE", "IFCNUMERICMEASURE"},
    "length": {"IFCLENGTHMEASURE", "IFCPOSITIVELENGTHMEASURE", "IFCNONNEGATIVELENGTHMEASURE"},
    "area": {"IFCAREAMEASURE", "IFCPOSITIVEAREAMEASURE"},
    "volume": {"IFCVOLUMEMEASURE"},
    "mass": {"IFCMASSMEASURE"},
    "time": {"IFCTIMEMEASURE", "IFCDURATION", "IFCLABEL"},
}

VENDOR_PSET_ALLOW_PREFIX = ("PSET_", "SGPSET_", "QTO_", "BASEQUANTITIES")

# A mapping edition older than this is reported as a caveat on DOC.001. The BCA
# workbook is versioned and labelled work-in-progress, so age matters.
STALE_MAPPING_DAYS = 365


class Finding(object):
    """One locatable observation attached to a rule.

    ifc_id is the entity a user should be taken to. extra_ids lists every other
    entity affected by the same issue, so one row can select a whole group.
    """

    __slots__ = ("rule_id", "severity", "message", "ifc_id", "entity",
                 "name", "guid", "detail", "extra_ids")

    def __init__(self, rule_id, severity, message, ifc_id=None, entity="",
                 name="", guid="", detail="", extra_ids=None):
        self.rule_id = rule_id
        self.severity = severity
        self.message = message
        self.ifc_id = ifc_id
        self.entity = entity
        self.name = name
        self.guid = guid
        self.detail = detail
        self.extra_ids = extra_ids or []

    @property
    def all_ids(self):
        ids = [self.ifc_id] if self.ifc_id is not None else []
        for i in self.extra_ids:
            if i != self.ifc_id:
                ids.append(i)
        return ids

    def as_dict(self):
        return {
            "rule": self.rule_id, "severity": self.severity, "message": self.message,
            "ifc_id": self.ifc_id, "entity": self.entity, "name": self.name,
            "guid": self.guid, "detail": self.detail,
            "affected_ids": self.all_ids,
        }


class RuleResult(object):
    __slots__ = ("rule_id", "title", "category", "declared_severity",
                 "status", "summary", "findings", "fix")

    def __init__(self, rule_id, title, category, declared_severity,
                 status, summary, findings=None, fix=""):
        self.rule_id = rule_id
        self.title = title
        self.category = category
        self.declared_severity = declared_severity
        self.status = status
        self.summary = summary
        self.findings = findings or []
        self.fix = fix

    @property
    def count(self):
        return len(self.findings)

    def as_dict(self):
        return {
            "rule": self.rule_id, "title": self.title, "category": self.category,
            "declared_severity": self.declared_severity, "status": self.status,
            "summary": self.summary, "fix": self.fix,
            "findings": [f.as_dict() for f in self.findings],
        }


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class Context(object):
    """Indexes derived once and shared by every rule."""

    def __init__(self, ifc, library, progress=None, cancel=None):
        self.f = ifc
        self.lib = library
        self.ifc4_layout = (ifc.schema or "").upper().startswith("IFC4")
        self.geo = spf.Geometry(ifc)
        self._progress = progress
        self._cancel = cancel

        self.pset_name = {}          # pset entity id -> name
        self.pset_props = {}         # pset entity id -> [(propId, name, typename, value)]
        self.occurrence_psets = {}   # object id -> {pset name: pset id}
        self.type_psets = {}         # type id  -> {pset name: pset id}
        self.object_type = {}        # object id -> type object id
        self.contained = {}          # element id -> spatial structure id
        self.decomposed = set()      # ids that are part of a decomposition
        self.aggregate_parent = {}   # child id -> parent id
        self.element_ids = []
        self.element_classes = set()
        self.geometric_ids = set()
        self.pset_owners = {}        # pset id -> [object ids carrying it]
        self.quantities = {}         # object id -> {quantity name: value}

        self._stage("Indexing property sets", 0, 4)
        self._index_psets()
        self._stage("Indexing relationships", 1, 4)
        self._index_relations()
        self._stage("Indexing model elements", 2, 4)
        self._index_elements()
        self._stage("Indexing property owners", 3, 4)
        self._index_owners()
        self._stage("Model index ready", 4, 4)

    def _check_cancel(self):
        if self._cancel is not None and self._cancel():
            raise spf.OperationCancelled("Operation cancelled.")

    def _stage(self, label, completed, total):
        self._check_cancel()
        if self._progress is not None:
            self._progress(label, completed, total)

    # -- indexing -----------------------------------------------------------

    def _index_psets(self):
        for ps in self.f.of_type("IFCPROPERTYSET"):
            self._check_cancel()
            name = ps.value(2)
            self.pset_name[ps.id] = name
            props = []
            for pid in spf.as_refs(ps.arg(4)):
                pe = self.f.get(pid)
                if pe is None or pe.type != "IFCPROPERTYSINGLEVALUE":
                    continue
                tname, val = spf.typed_value(pe.arg(2))
                props.append((pid, pe.value(0), tname, val))
            self.pset_props[ps.id] = props

        for eq in self.f.of_type("IFCELEMENTQUANTITY"):
            self._check_cancel()
            self.pset_name[eq.id] = eq.value(2) or "IfcElementQuantity"

    def _index_relations(self):
        for rel in self.f.of_type("IFCRELDEFINESBYPROPERTIES"):
            self._check_cancel()
            pd = spf.as_ref(rel.arg(5))
            if pd is None:
                continue
            nm = self.pset_name.get(pd) or self.f.type_of(pd) or "?"
            for oid in spf.as_refs(rel.arg(4)):
                self.occurrence_psets.setdefault(oid, {})[nm] = pd

        for rel in self.f.of_type("IFCRELDEFINESBYTYPE"):
            self._check_cancel()
            tid = spf.as_ref(rel.arg(5))
            for oid in spf.as_refs(rel.arg(4)):
                if tid is not None:
                    self.object_type[oid] = tid

        # Type objects are a tiny subset of a real IFC. Walking all 3M+ geometry
        # entities here added seconds and memory pressure for no useful work.
        for etype, entities in self.f._index().items():
            if not etype.endswith("TYPE") or etype.startswith("IFCREL"):
                continue
            for ent in entities:
                self._check_cancel()
                args = ent.args
                if len(args) < 6:
                    continue
                bucket = self.type_psets.setdefault(ent.id, {})
                for pid in spf.as_refs(args[5]):
                    nm = self.pset_name.get(pid) or self.f.type_of(pid) or "?"
                    bucket[nm] = pid

        for rel in self.f.of_type("IFCRELCONTAINEDINSPATIALSTRUCTURE"):
            self._check_cancel()
            struct = spf.as_ref(rel.arg(5))
            for oid in spf.as_refs(rel.arg(4)):
                self.contained[oid] = struct

        for rel in self.f.of_type("IFCRELAGGREGATES"):
            self._check_cancel()
            parent = spf.as_ref(rel.arg(4))
            for oid in spf.as_refs(rel.arg(5)):
                self.decomposed.add(oid)
                self.aggregate_parent[oid] = parent
        for rel in self.f.of_type("IFCRELNESTS"):
            self._check_cancel()
            for oid in spf.as_refs(rel.arg(5)):
                self.decomposed.add(oid)
        for rel in self.f.of_type("IFCRELVOIDSELEMENT"):
            self._check_cancel()
            oid = spf.as_ref(rel.arg(5))
            if oid is not None:
                self.decomposed.add(oid)

        for rel in self.f.of_type("IFCRELDEFINESBYPROPERTIES"):
            self._check_cancel()
            pd = spf.as_ref(rel.arg(5))
            pe = self.f.get(pd) if pd else None
            if pe is None or pe.type != "IFCELEMENTQUANTITY":
                continue
            vals = {}
            for qid in spf.as_refs(pe.arg(5) if len(pe.args) > 5 else pe.arg(4)):
                qe = self.f.get(qid)
                if qe is None or not qe.type.startswith("IFCQUANTITY"):
                    continue
                qv = spf.as_float(qe.arg(3))
                if qe.value(0) is not None and qv is not None:
                    vals[qe.value(0)] = qv
            for oid in spf.as_refs(rel.arg(4)):
                self.quantities.setdefault(oid, {}).update(vals)

    def _index_elements(self):
        classes = set()
        for rel in self.f.of_type("IFCRELCONTAINEDINSPATIALSTRUCTURE"):
            self._check_cancel()
            for oid in spf.as_refs(rel.arg(4)):
                t = self.f.type_of(oid)
                if t:
                    classes.add(t)
        for rel in self.f.of_type("IFCRELAGGREGATES"):
            self._check_cancel()
            parent = spf.as_ref(rel.arg(4))
            ptype = self.f.type_of(parent)
            if ptype in SPATIAL_CLASSES:
                continue
            for oid in spf.as_refs(rel.arg(5)):
                t = self.f.type_of(oid)
                if t:
                    classes.add(t)
        classes.update(spf.PREDEFINED_TYPE_INDEX.keys())
        for ent in self.lib.known_entities():
            classes.add(ent.upper())
        classes -= NON_ELEMENT_CLASSES
        classes = set(c for c in classes if not c.endswith("TYPE"))

        present = []
        for cls in sorted(classes):
            self._check_cancel()
            for ent in self.f.of_type(cls):
                present.append(ent)
        present.sort(key=lambda e: e.id)
        self.element_ids = present
        self.element_classes = set(e.type for e in present)
        self.geometric_ids = set(e.id for e in present)
        self.geometric_ids.update(e.id for e in self.f.of_type("IFCSPACE"))

    def _index_owners(self):
        owners = {}
        for oid, mapping in self.occurrence_psets.items():
            self._check_cancel()
            for _nm, pid in mapping.items():
                owners.setdefault(pid, set()).add(oid)
        for oid, tid in self.object_type.items():
            self._check_cancel()
            for _nm, pid in self.type_psets.get(tid, {}).items():
                owners.setdefault(pid, set()).add(oid)
        for tid, mapping in self.type_psets.items():
            for _nm, pid in mapping.items():
                if pid not in owners:
                    owners[pid] = {tid}
        self.pset_owners = dict((k, sorted(v)) for k, v in owners.items())

    # -- helpers ------------------------------------------------------------

    def psets_of(self, oid):
        """Occurrence property sets plus those inherited from the type."""
        out = {}
        tid = self.object_type.get(oid)
        if tid is not None:
            out.update(self.type_psets.get(tid, {}))
        out.update(self.occurrence_psets.get(oid, {}))
        return out

    def sg_psets_of(self, oid):
        return dict((k, v) for k, v in self.psets_of(oid).items()
                    if k and k.upper().startswith("SGPSET_"))

    def predefined_type(self, ent):
        """Return (value, known_layout). value is None when the attribute is unset."""
        idx = spf.PREDEFINED_TYPE_INDEX.get(ent.type, "missing")
        if idx == "missing":
            return (None, False)
        if idx is None:
            return (None, True)
        return (ent.value(idx), True)

    def secondary_type(self, ent):
        spec = spf.SECONDARY_TYPE_INDEX.get(ent.type)
        if not spec:
            return (None, None)
        return (spec[0], ent.value(spec[1]))

    def label(self, ent):
        return ent.value(spf.IDX_NAME) or ent.value(spf.IDX_OBJECTTYPE) or ""

    def guid(self, ent):
        return ent.value(spf.IDX_GLOBALID) or ""

    def storey_of(self, oid):
        """Nearest IfcBuildingStorey above an element, walking through spaces."""
        seen = set()
        cur = self.contained.get(oid) or self.aggregate_parent.get(oid)
        while cur is not None and cur not in seen:
            seen.add(cur)
            if self.f.type_of(cur) == "IFCBUILDINGSTOREY":
                return cur
            cur = self.contained.get(cur) or self.aggregate_parent.get(cur)
        return None

    def storey_label(self, oid):
        sid = self.storey_of(oid)
        if sid is None:
            return "(no storey)"
        ent = self.f.get(sid)
        if ent is None:
            return "(no storey)"
        return self.label(ent) or "(unnamed storey)"

    def finding(self, rule_id, severity, message, ent=None, detail=""):
        if ent is None:
            return Finding(rule_id, severity, message, detail=detail)
        return Finding(rule_id, severity, message, ifc_id=ent.id, entity=ent.type,
                       name=self.label(ent), guid=self.guid(ent), detail=detail)

    def owners_of(self, pset_ids):
        """Objects carrying any of these property sets, geometric ones first.

        Sets attached only to the project, site, building, storey or a type are
        still tied to that object rather than reported as objectless.
        """
        out = set()
        others = set()
        for pid in pset_ids:
            for oid in self.pset_owners.get(pid, []):
                (out if oid in self.geometric_ids else others).add(oid)
        return sorted(out) or sorted(others)

    def is_part(self, oid):
        """True for a component part aggregated under a non-spatial element."""
        parent = self.aggregate_parent.get(oid)
        return parent is not None and self.f.type_of(parent) not in SPATIAL_CLASSES

    def property_finding(self, rule_id, severity, message, pset_ids,
                         pset_name="", detail=""):
        """A property-set defect, attached to the elements that actually carry it."""
        ids = self.owners_of(pset_ids)
        rep = self.f.get(ids[0]) if ids else None
        bits = [d for d in (detail,) if d]
        if ids:
            bits.append("affects %d element(s)" % len(ids))
        else:
            bits.append("the property set is not attached to any object in the file")
        if pset_name:
            bits.append("in %s" % pset_name)
        return Finding(
            rule_id, severity, message,
            ifc_id=rep.id if rep is not None else None,
            entity=rep.type if rep is not None else "IfcPropertySet",
            name=self.label(rep) if rep is not None else pset_name,
            guid=self.guid(rep) if rep is not None else "",
            detail="  |  ".join(bits),
            extra_ids=ids)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine(object):
    def __init__(self, library):
        self.lib = library

    def run(self, ifc, progress=None, cancel=None):
        ctx = Context(ifc, self.lib, progress=progress, cancel=cancel)
        results = []
        rules = (
            _schema_001, _schema_002, _schema_003,
            _geo_001, _geo_002, _geo_003, _geo_004,
            _spatial_001, _spatial_002, _spatial_003, _spatial_004,
            _class_001, _class_002, _class_003,
            _pset_001, _pset_002, _pset_003, _pset_004, _pset_005,
            _pset_006, _pset_007, _pset_008,
            _id_001, _id_002,
            _unit_001,
            _qc_001, _qc_002,
            _doc_001,
        )
        for index, fn in enumerate(rules):
            if cancel is not None and cancel():
                raise spf.OperationCancelled("Operation cancelled.")
            if progress is not None:
                progress("Running %s" % getattr(fn, "rule_id", fn.__name__),
                         index, len(rules))
            try:
                results.append(fn(ctx))
            except spf.OperationCancelled:
                raise
            except Exception as exc:  # a broken rule must not hide the others
                results.append(RuleResult(
                    getattr(fn, "rule_id", fn.__name__), "Rule execution error",
                    "engine", "ERROR", UNKNOWN,
                    "Rule raised %s: %s" % (type(exc).__name__, exc)))
        if progress is not None:
            progress("Rules complete", len(rules), len(rules))
        return ctx, results

def _mk(ctx, rule_id, status, summary, findings=None):
    meta = ctx.lib.rule(rule_id)
    return RuleResult(
        rule_id,
        meta.get("title", rule_id),
        meta.get("category", ""),
        meta.get("severity", "ERROR"),
        status,
        summary,
        findings or [],
        meta.get("fix", ""),
    )


# -- schema -----------------------------------------------------------------

def _schema_001(ctx):
    schema = (ctx.f.schema or "").upper()
    if schema == "IFC4":
        return _mk(ctx, "SCHEMA.001", PASS, "FILE_SCHEMA is IFC4.")
    if not schema:
        return _mk(ctx, "SCHEMA.001", FAIL, "FILE_SCHEMA is missing from the header.")
    return _mk(ctx, "SCHEMA.001", FAIL,
               "FILE_SCHEMA is '%s'. IFC+SG accepts IFC4 only." % schema)


def _schema_002(ctx):
    views = ctx.f.view_definitions
    if not views:
        return _mk(ctx, "SCHEMA.002", FAIL,
                   "No ViewDefinition declared in FILE_DESCRIPTION.")
    joined = " ".join(views).upper()
    if "REFERENCEVIEW" in joined:
        return _mk(ctx, "SCHEMA.002", PASS, "MVD declared: %s" % "; ".join(views))
    return _mk(ctx, "SCHEMA.002", FAIL,
               "MVD is '%s'. IFC+SG requires the IFC4 Reference View." % "; ".join(views))


def _schema_003(ctx):
    if ctx.f.parse_error:
        return _mk(ctx, "SCHEMA.003", FAIL, "Parse error: %s" % ctx.f.parse_error)
    n = ctx.f.count()
    if n == 0:
        return _mk(ctx, "SCHEMA.003", FAIL, "No entities were read from the DATA section.")
    return _mk(ctx, "SCHEMA.003", PASS, "Parsed %d entity instances without structural error." % n)


# -- georeferencing ---------------------------------------------------------

def _map_conversions(ctx):
    out = []
    for mc in ctx.f.of_type("IFCMAPCONVERSION"):
        crs_id = spf.as_ref(mc.arg(1))
        crs = ctx.f.get(crs_id) if crs_id else None
        out.append((mc, crs))
    return out


def _geo_001(ctx):
    mcs = _map_conversions(ctx)
    if not mcs:
        return _mk(ctx, "GEO.001", FAIL, "No IfcMapConversion in the file.")
    bad = [mc for mc, crs in mcs if crs is None or crs.type != "IFCPROJECTEDCRS"]
    if bad:
        return _mk(ctx, "GEO.001", FAIL,
                   "%d IfcMapConversion do not reference an IfcProjectedCRS." % len(bad),
                   [ctx.finding("GEO.001", "ERROR", "TargetCRS is not an IfcProjectedCRS.", mc)
                    for mc in bad])
    return _mk(ctx, "GEO.001", PASS,
               "%d IfcMapConversion present, each referencing an IfcProjectedCRS." % len(mcs))


def _geo_002(ctx):
    mcs = _map_conversions(ctx)
    if not mcs:
        return _mk(ctx, "GEO.002", UNKNOWN, "No IfcMapConversion to inspect.")
    findings = []
    for mc, crs in mcs:
        if crs is None:
            continue
        blob = " ".join(str(crs.value(i) or "") for i in range(0, 7)).upper()
        if "3414" in blob or "SVY21" in blob or "SVY 21" in blob:
            continue
        findings.append(ctx.finding(
            "GEO.002", "ERROR",
            "Projected CRS does not identify SVY21 / EPSG:3414.", crs,
            detail="Name=%s GeodeticDatum=%s MapProjection=%s MapZone=%s" % (
                crs.value(0), crs.value(2), crs.value(4), crs.value(5))))
    if findings:
        return _mk(ctx, "GEO.002", FAIL, "Projected CRS is not SVY21.", findings)
    crs = mcs[0][1]
    return _mk(ctx, "GEO.002", PASS,
               "Projected CRS identifies SVY21 / EPSG:3414 (Name=%s, GeodeticDatum=%s)."
               % (crs.value(0), crs.value(2)))


def _geo_003(ctx):
    mcs = _map_conversions(ctx)
    if not mcs:
        return _mk(ctx, "GEO.003", UNKNOWN, "No IfcMapConversion to inspect.")
    findings = []
    for mc, crs in mcs:
        vdatum = (crs.value(3) if crs is not None else None) or ""
        blob = vdatum.upper().replace(" ", "")
        if "SHD" in blob or "SINGAPOREHEIGHT" in blob:
            continue
        findings.append(ctx.finding(
            "GEO.003", "ERROR",
            "VerticalDatum is '%s'; Singapore Height Datum is not declared." % (vdatum or "unset"),
            crs if crs is not None else mc,
            detail="IfcMapConversion.OrthogonalHeight = %s" % (mc.value(4),)))
    if findings:
        return _mk(ctx, "GEO.003", FAIL,
                   "Vertical datum does not declare Singapore Height Datum.", findings)
    return _mk(ctx, "GEO.003", PASS, "VerticalDatum declares Singapore Height Datum.")


def _geo_004(ctx):
    mcs = _map_conversions(ctx)
    if not mcs:
        return _mk(ctx, "GEO.004", UNKNOWN, "No IfcMapConversion to report.")
    lines = []
    for mc, crs in mcs:
        lines.append("E=%s N=%s H=%s XAxisAbscissa=%s XAxisOrdinate=%s Scale=%s" % (
            mc.value(2), mc.value(3), mc.value(4), mc.value(5), mc.value(6), mc.value(7)))
    return _mk(ctx, "GEO.004", NOT_APPLICABLE,
               "Single file checked; cross-discipline comparison needs the other "
               "discipline models. Values to compare: " + " | ".join(lines))


# -- spatial ----------------------------------------------------------------

def _spatial_001(ctx):
    missing = []
    for cls in ("IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY"):
        if not ctx.f.of_type(cls):
            missing.append(cls)
    if missing:
        return _mk(ctx, "SPATIAL.001", FAIL,
                   "Missing spatial container(s): %s" % ", ".join(missing))

    parents = ctx.aggregate_parent
    findings = []
    for cls, wanted in (("IFCSITE", "IFCPROJECT"),
                        ("IFCBUILDING", "IFCSITE"),
                        ("IFCBUILDINGSTOREY", "IFCBUILDING")):
        for ent in ctx.f.of_type(cls):
            ptype = ctx.f.type_of(parents.get(ent.id))
            if ptype != wanted:
                findings.append(ctx.finding(
                    "SPATIAL.001", "ERROR",
                    "%s is aggregated under %s, expected %s." % (cls, ptype or "nothing", wanted),
                    ent))
    if findings:
        return _mk(ctx, "SPATIAL.001", FAIL, "Spatial aggregation is incomplete.", findings)
    return _mk(ctx, "SPATIAL.001", PASS,
               "IfcProject > IfcSite > IfcBuilding > IfcBuildingStorey present and aggregated "
               "(%d site, %d building, %d storey)." % (
                   len(ctx.f.of_type("IFCSITE")), len(ctx.f.of_type("IFCBUILDING")),
                   len(ctx.f.of_type("IFCBUILDINGSTOREY"))))


def _spatial_002(ctx):
    findings = []
    for ent in ctx.element_ids:
        if ent.id in ctx.contained or ent.id in ctx.decomposed:
            continue
        findings.append(ctx.finding(
            "SPATIAL.002", "ERROR",
            "Not contained in any spatial structure and not part of a decomposition.", ent))
    total = len(ctx.element_ids)
    if findings:
        return _mk(ctx, "SPATIAL.002", FAIL,
                   "%d of %d elements are orphaned." % (len(findings), total), findings)
    return _mk(ctx, "SPATIAL.002", PASS,
               "All %d elements are spatially contained or decomposed." % total)


def _spatial_003(ctx):
    storeys = ctx.f.of_type("IFCBUILDINGSTOREY")
    if not storeys:
        return _mk(ctx, "SPATIAL.003", UNKNOWN, "No IfcBuildingStorey present.")
    findings = []
    seen = {}
    for st in storeys:
        nm = st.value(2)
        if not nm:
            findings.append(ctx.finding("SPATIAL.003", "WARN", "Storey has no Name.", st))
            continue
        if nm in seen:
            findings.append(ctx.finding(
                "SPATIAL.003", "WARN",
                "Storey name '%s' is used more than once." % nm, st))
        seen[nm] = st.id
    if len(storeys) == 1:
        findings.append(ctx.finding(
            "SPATIAL.003", "WARN",
            "Only one IfcBuildingStorey ('%s', elevation %s). Confirm the model is "
            "genuinely single-storey." % (storeys[0].value(2), storeys[0].value(9)),
            storeys[0]))
    if findings:
        return _mk(ctx, "SPATIAL.003", WARN,
                   "%d storey naming item(s) need QP judgement." % len(findings), findings)
    return _mk(ctx, "SPATIAL.003", PASS,
               "%d storeys, all named and distinct." % len(storeys))


def _spatial_004(ctx):
    buildings = ctx.f.of_type("IFCBUILDING")
    if len(buildings) <= 1:
        return _mk(ctx, "SPATIAL.004", PASS,
                   "Single IfcBuilding ('%s'); the Block Mechanism is not engaged. "
                   "Cross-discipline block consistency cannot be checked from one file."
                   % (buildings[0].value(2) if buildings else "-"))
    findings = [ctx.finding("SPATIAL.004", "INFO",
                            "Block: %s" % (b.value(2) or "unnamed"), b) for b in buildings]
    return _mk(ctx, "SPATIAL.004", UNKNOWN,
               "%d IfcBuilding blocks present. Consistency across disciplines requires "
               "the other discipline models." % len(buildings), findings)


# -- classification ---------------------------------------------------------

def _class_001(ctx):
    proxies = ctx.f.of_type("IFCBUILDINGELEMENTPROXY")
    if not proxies:
        return _mk(ctx, "CLASS.001", PASS, "No IfcBuildingElementProxy in the model.")
    mapped = ctx.lib.valid_subtypes("IFCBUILDINGELEMENTPROXY")
    findings = []
    parts = 0
    for p in proxies:
        obj_type = p.value(spf.IDX_OBJECTTYPE) or ""
        token = re.sub(r"\s+", "", obj_type).upper().lstrip("*")
        if token and token in mapped:
            continue
        if ctx.is_part(p.id):
            parts += 1
            continue
        parent = ctx.f.type_of(ctx.aggregate_parent.get(p.id)) or ""
        findings.append(ctx.finding(
            "CLASS.001", "ERROR",
            "IfcBuildingElementProxy without a mapped IFC+SG subtype token.",
            p, detail="ObjectType=%s%s. Reclassify to the correct IFC entity, or set the "
                      "ObjectType token of the identified component it represents."
                      % (obj_type or "unset", (" part of " + parent) if parent else "")))
    accepted = len(proxies) - len(findings) - parts
    part_note = ("" if not parts else
                 " %d further proxies are parts of an aggregate such as a curtain wall; "
                 "they take the parent's classification and are not listed." % parts)
    if not findings:
        return _mk(ctx, "CLASS.001", PASS,
                   "No stand-alone proxy without a mapped IFC+SG subtype token (%d mapped)."
                   % accepted + part_note)
    return _mk(ctx, "CLASS.001", FAIL,
               "%d stand-alone IfcBuildingElementProxy elements carry no mapped IFC+SG "
               "subtype token; %d do.%s" % (len(findings), accepted, part_note), findings)


def _not_ifc4(ctx, rule_id):
    return _mk(ctx, rule_id, NOT_APPLICABLE,
               "The file is %s. Subtype attributes sit at different positions outside "
               "IFC4, so reading them would report wrong values. Fix SCHEMA.001 "
               "(export IFC4 Reference View) first." % (ctx.f.schema or "not IFC4"))


def _class_002(ctx):
    if not ctx.ifc4_layout:
        return _not_ifc4(ctx, "CLASS.002")
    findings = []
    unknown_layout = {}
    checked = 0
    for ent in ctx.element_ids:
        if ent.type == "IFCBUILDINGELEMENTPROXY" and ctx.is_part(ent.id):
            continue
        value, known = ctx.predefined_type(ent)
        if not known:
            unknown_layout[ent.type] = unknown_layout.get(ent.type, 0) + 1
            continue
        checked += 1
        obj_type = ent.value(spf.IDX_OBJECTTYPE)
        if value is None:
            findings.append(ctx.finding(
                "CLASS.002", "ERROR", "PredefinedType attribute is unset.", ent,
                detail="ObjectType=%s" % (obj_type or "unset")))
        elif value.upper() == "NOTDEFINED":
            findings.append(ctx.finding(
                "CLASS.002", "ERROR", "PredefinedType is NOTDEFINED.", ent,
                detail="ObjectType=%s" % (obj_type or "unset")))
        elif value.upper() == "USERDEFINED" and not obj_type:
            findings.append(ctx.finding(
                "CLASS.002", "ERROR",
                "PredefinedType is USERDEFINED but ObjectType is empty.", ent))
        sec_name, sec_val = ctx.secondary_type(ent)
        if sec_name and (sec_val is None or sec_val.upper() == "NOTDEFINED"):
            findings.append(ctx.finding(
                "CLASS.002", "WARN",
                "%s is %s." % (sec_name, sec_val or "unset"), ent))

    note = ""
    if unknown_layout:
        note = " Attribute layout unknown for: " + ", ".join(
            "%s x%d" % (k, v) for k, v in sorted(unknown_layout.items()))
    if findings:
        errs = sum(1 for f in findings if f.severity == "ERROR")
        return _mk(ctx, "CLASS.002", FAIL,
                   "%d of %d elements have no usable subtype declared.%s"
                   % (errs, checked, note), findings)
    return _mk(ctx, "CLASS.002", PASS,
               "All %d checked elements declare a subtype.%s" % (checked, note))


def _class_003(ctx):
    if not ctx.ifc4_layout:
        return _not_ifc4(ctx, "CLASS.003")
    findings = []
    checked = 0
    unresolved = 0
    tokens = ctx.lib.area_scheme_tokens()

    for ent in ctx.f.of_type("IFCSPACE"):
        value, _known = ctx.predefined_type(ent)
        obj_type = ent.value(spf.IDX_OBJECTTYPE)
        checked += 1
        if (value or "").upper() != "USERDEFINED":
            findings.append(ctx.finding(
                "CLASS.003", "ERROR",
                "IfcSpace PredefinedType is '%s'. A regulated area object must be "
                "USERDEFINED with an IFC+SG area token in ObjectType." % (value or "unset"),
                ent, detail="ObjectType=%s. Valid tokens: %s"
                            % (obj_type or "unset", ", ".join(tokens))))
            continue
        scheme = ctx.lib.area_scheme_by_token(obj_type)
        if scheme is None:
            findings.append(ctx.finding(
                "CLASS.003", "ERROR",
                "ObjectType token '%s' is not an IFC+SG area scheme token." % (obj_type or "unset"),
                ent, detail="Valid tokens: %s" % ", ".join(tokens)))

    for ent in ctx.element_ids:
        value, known = ctx.predefined_type(ent)
        if not known or (value or "").upper() != "USERDEFINED":
            continue
        checked += 1
        obj_type = ent.value(spf.IDX_OBJECTTYPE)
        valid = ctx.lib.valid_subtypes(ent.type)
        if not valid:
            continue
        token = re.sub(r"\s+", "", str(obj_type or "")).upper().lstrip("*")
        if token and token in valid:
            continue
        findings.append(ctx.finding(
            "CLASS.003", "WARN",
            "USERDEFINED token '%s' is not a subtype the mapping lists for %s."
            % (obj_type or "", ent.type), ent,
            detail="Mapped tokens: %s" % (", ".join(sorted(valid)[:25]) or "none")))

    if findings:
        return _mk(ctx, "CLASS.003", FAIL,
                   "%d subtype token(s) invalid or not declared." % len(findings), findings)
    if unresolved:
        return _mk(ctx, "CLASS.003", UNKNOWN,
                   "%d USERDEFINED tokens cannot be verified: the mapping library is a "
                   "partial seed. Build the full library to resolve them." % unresolved)
    return _mk(ctx, "CLASS.003", PASS,
               "All %d checked subtype tokens resolve against the library." % checked)


# -- property sets ----------------------------------------------------------

def _subtype_token(ctx, ent):
    """The IFC+SG subtype token of an element: PredefinedType or *ObjectType."""
    if not ctx.ifc4_layout:
        return None
    value, known = ctx.predefined_type(ent)
    obj_type = ent.value(spf.IDX_OBJECTTYPE)
    if known and value and value.upper() not in ("NOTDEFINED", "USERDEFINED"):
        return value
    if obj_type and (not known or (value or "").upper() == "USERDEFINED"):
        return "*" + obj_type.lstrip("*")
    return None


def _pset_001(ctx):
    """Every element of an identified component carries that component's sets.

    The workbook lists a component's sets without their applicability, which the
    COP gives per property ("All walls", "RC walls", "When required"). So only
    the component's own SGPset_<Entity> - or, failing that, every listed set - is
    unconditional and an ERROR when absent; other absent sets are a WARN to
    confirm against the COP (for example reinforcement on a steel beam).
    """
    findings = []
    checked = 0
    unmapped = {}
    skipped_proxies = 0
    errors = 0
    for ent in ctx.element_ids + ctx.f.of_type("IFCSPACE"):
        token = _subtype_token(ctx, ent)
        scheme = (ctx.lib.area_scheme_by_token(ent.value(spf.IDX_OBJECTTYPE))
                  if ent.type == "IFCSPACE" and ctx.ifc4_layout else None)
        if scheme is not None and scheme.get("property_set"):
            required = ([scheme["property_set"]],
                        "area scheme %s" % scheme.get("object_type_token"))
        else:
            required = ctx.lib.required_psets(ent.type, token)
            if (required is not None and ent.type == "IFCBUILDINGELEMENTPROXY"
                    and "identified component" not in required[1]):
                # A proxy is only a component through its token; CLASS.001 reports it.
                skipped_proxies += 1
                continue
        if required is None:
            unmapped[ent.type] = unmapped.get(ent.type, 0) + 1
            continue
        sets, basis = required
        if not sets:
            continue
        checked += 1
        present = ctx.psets_of(ent.id)
        missing = [p for p in sets if p not in present]
        if not missing:
            continue
        main = "SGPSET_" + base_entity(ent.type)[3:]
        core = [p for p in missing if p.upper() == main] or (
            missing if len(missing) == len(sets) else [])
        if core:
            errors += 1
            findings.append(ctx.finding(
                "PSET.001", "ERROR",
                "Missing required property set(s): %s" % ", ".join(missing), ent,
                detail="Required by %s." % basis))
        else:
            findings.append(ctx.finding(
                "PSET.001", "WARN",
                "Missing %s - confirm whether it applies to this element in the COP."
                % ", ".join(missing), ent,
                detail="Listed for %s; the COP limits some sets to certain element types "
                       "(for example RC or precast)." % basis))

    notes = []
    if skipped_proxies:
        notes.append(" %d proxies without a mapped subtype are reported by CLASS.001 only."
                     % skipped_proxies)
    if unmapped:
        notes.append(" Not checked, because the IFC+SG mapping has no identified "
                     "component for these classes: %s."
                     % ", ".join("%s x%d" % (k, v) for k, v in sorted(unmapped.items())))
    if not any(n and n.upper().startswith("SGPSET_") for n in ctx.pset_name.values()):
        notes.append(" This file contains no SGPset at all: it was not exported with an "
                     "IFC+SG configuration, which is the single cause of these findings.")
    if not ctx.ifc4_layout:
        notes.append(" Subtypes are not read from a %s file, so only the sets common to "
                     "every candidate component are required." % (ctx.f.schema or "non-IFC4"))
    note = "".join(notes)
    warns = len(findings) - errors
    if errors:
        return _mk(ctx, "PSET.001", FAIL,
                   "%d of %d checked elements lack their component's required property "
                   "set; %d more lack a set that may be conditional.%s"
                   % (errors, checked, warns, note), findings)
    if findings:
        return _mk(ctx, "PSET.001", WARN,
                   "%d of %d checked elements lack a property set that may be conditional."
                   "%s" % (warns, checked, note), findings)
    return _mk(ctx, "PSET.001", PASS,
               "Required property sets present on all %d checked elements.%s"
               % (checked, note))


class _Groups(object):
    """Collect one row per distinct defect, remembering every property set instance."""

    def __init__(self):
        self.rows = {}
        self.order = []

    def add(self, key, severity, message, pset_id, pset_name="", detail=""):
        row = self.rows.get(key)
        if row is None:
            row = {"severity": severity, "message": message, "detail": detail,
                   "pset_name": pset_name, "psets": set()}
            self.rows[key] = row
            self.order.append(key)
        row["psets"].add(pset_id)

    def emit(self, ctx, rule_id):
        out = []
        for key in sorted(self.order):
            row = self.rows[key]
            out.append(ctx.property_finding(
                rule_id, row["severity"], row["message"], row["psets"],
                pset_name=row["pset_name"], detail=row["detail"]))
        return out

    def __len__(self):
        return len(self.rows)

    def count(self, severity):
        return sum(1 for r in self.rows.values() if r["severity"] == severity)


def _sg_psets(ctx):
    for pid, name in sorted(ctx.pset_name.items()):
        if name and name.upper().startswith("SGPSET_"):
            yield pid, name


def _pset_002(ctx):
    incomplete = not ctx.lib.is_complete
    severity = "WARN" if incomplete else "ERROR"
    groups = _Groups()
    for pid, name in _sg_psets(ctx):
        for _propid, pname, _t, _v in ctx.pset_props.get(pid, []):
            if not pname:
                continue
            owners = ctx.lib.owning_psets(pname)
            if not owners or name in owners:
                continue
            msg = ("Property '%s' sits in '%s'; the loaded mapping assigns it to %s."
                   % (pname, name, " / ".join(owners)))
            if incomplete:
                msg += (" The library is a partial seed, so '%s' may legitimately own this "
                        "name too. Build the full library to decide." % name)
            groups.add((name, pname), severity, msg, pid, pset_name=name)

    findings = groups.emit(ctx, "PSET.002")
    if findings and not incomplete:
        return _mk(ctx, "PSET.002", FAIL,
                   "%d properties sit in the wrong property set." % len(findings), findings)
    if findings:
        return _mk(ctx, "PSET.002", UNKNOWN,
                   "%d property placements cannot be confirmed against a partial mapping "
                   "library." % len(findings), findings)
    if incomplete:
        return _mk(ctx, "PSET.002", UNKNOWN,
                   "No misplacement found among resolvable properties, but the mapping "
                   "library is a partial seed so placement is unconfirmed elsewhere.")
    return _mk(ctx, "PSET.002", PASS, "Every property sits in its mapped property set.")


def _pset_003(ctx):
    groups = _Groups()
    resolvable = 0
    for pid, name in _sg_psets(ctx):
        res_set = ctx.lib.resolve_pset(name)
        if res_set.status in (CASE_MISMATCH, NEAR_MISS):
            groups.add(("A", name), "ERROR",
                       "Property set name '%s' does not match the mapping (%s). Closest: %s"
                       % (name, res_set.status, ", ".join(res_set.candidates) or "-"),
                       pid, pset_name=name)
            continue
        if res_set.status != EXACT:
            continue
        for _propid, pname, _t, _v in ctx.pset_props.get(pid, []):
            if not pname:
                continue
            resolvable += 1
            if pname != pname.strip():
                groups.add(("B", name, pname), "ERROR",
                           "Property '%s' in '%s' has leading or trailing whitespace."
                           % (pname, name), pid, pset_name=name)
                continue
            res = ctx.lib.resolve_property(name, pname)
            if res.status == EXACT:
                continue
            if res.status != EXACT and ctx.lib.owning_psets(pname):
                # The exact name exists in another set: PSET.002 reports where it belongs.
                continue
            if res.status == RES_UNKNOWN:
                groups.add(("D", name, pname), "WARN",
                           "Property '%s' in '%s' is not in the loaded mapping. %s"
                           % (pname, name,
                              res.note or "Confirm against the full mapping workbook."),
                           pid, pset_name=name)
            else:
                groups.add(("C", name, pname), "ERROR",
                           "Property '%s' in '%s' is a %s of '%s'."
                           % (pname, name, res.status, res.value),
                           pid, pset_name=name,
                           detail="Candidates: %s" % (", ".join(res.candidates) or "-"))

    findings = groups.emit(ctx, "PSET.003")
    errs = groups.count("ERROR")
    if errs:
        return _mk(ctx, "PSET.003", FAIL,
                   "%d property/set names do not match the mapping exactly." % errs, findings)
    if findings:
        return _mk(ctx, "PSET.003", WARN,
                   "%d names could not be confirmed against the partial mapping library."
                   % len(findings), findings)
    return _mk(ctx, "PSET.003", PASS,
               "All %d resolvable property names match the mapping exactly." % resolvable)


def _pset_004(ctx):
    groups = _Groups()
    checked = 0
    for pid, name in _sg_psets(ctx):
        if ctx.lib.resolve_pset(name).status != EXACT:
            continue
        for _propid, pname, tname, _v in ctx.pset_props.get(pid, []):
            declared = [d for d in ctx.lib.property_datatypes(name, pname) if d]
            if not declared:
                continue
            checked += 1
            accepts = set()
            for item in declared:
                accepts |= DATATYPE_ACCEPTS.get(str(item).lower(), set())
            if not accepts or tname is None or tname.upper() in accepts:
                continue
            groups.add((name, pname, tname), "ERROR",
                       "%s.%s is declared %s in the mapping but exported as %s."
                       % (name, pname, " or ".join(declared), tname), pid, pset_name=name,
                       detail="Accepted IFC value types: %s" % ", ".join(sorted(accepts)))
    findings = groups.emit(ctx, "PSET.004")
    if findings:
        return _mk(ctx, "PSET.004", FAIL,
                   "%d properties use the wrong IFC value type." % len(findings), findings)
    return _mk(ctx, "PSET.004", PASS,
               "All %d type-resolvable property values use an accepted IFC type." % checked)


def _pset_005(ctx):
    groups = _Groups()
    for pid, name in _sg_psets(ctx):
        for _propid, pname, tname, value in ctx.pset_props.get(pid, []):
            if value is None or str(value).strip() == "":
                groups.add(("A", name, pname), "ERROR",
                           "%s.%s has no value." % (name, pname), pid, pset_name=name)
                continue
            sval = str(value).strip()
            if sval.lower() in PLACEHOLDER_VALUES:
                groups.add(("B", name, pname), "ERROR",
                           "%s.%s carries placeholder text '%s'." % (name, pname, sval),
                           pid, pset_name=name)
                continue
            if tname and tname.upper() not in ("IFCBOOLEAN", "IFCLOGICAL"):
                try:
                    if float(sval) == 0.0:
                        groups.add(("C", name, pname), "WARN",
                                   "%s.%s is zero on every instance checked; confirm this is "
                                   "measured data and not an unset default." % (name, pname),
                                   pid, pset_name=name)
                except ValueError:
                    pass

    findings = groups.emit(ctx, "PSET.005")
    errs = groups.count("ERROR")
    if errs:
        return _mk(ctx, "PSET.005", FAIL,
                   "%d SGPset properties are empty or hold placeholder text; %d are zero."
                   % (errs, groups.count("WARN")), findings)
    if findings:
        return _mk(ctx, "PSET.005", WARN,
                   "%d SGPset properties are zero-valued." % len(findings), findings)
    return _mk(ctx, "PSET.005", PASS, "No empty or placeholder SGPset values found.")


def _pset_006(ctx):
    groups = _Groups()
    controlled = 0
    for pid, name in _sg_psets(ctx):
        if ctx.lib.sgpsets.get(name) is None:
            continue
        scheme = None
        for s in ctx.lib.area_schemes():
            if s.get("property_set") == name:
                scheme = s
                break
        samples = (scheme or {}).get("sample_values", {}) or {}
        for _propid, pname, _t, value in ctx.pset_props.get(pid, []):
            if not ctx.lib.property_is_controlled(name, pname):
                continue
            controlled += 1
            allowed = samples.get(pname)
            if not allowed:
                groups.add(("A", name, pname), "WARN",
                           "%s.%s is a controlled-value property but the loaded library holds "
                           "no authoritative value list, so '%s' cannot be verified."
                           % (name, pname, value), pid, pset_name=name)
            elif value not in allowed:
                groups.add(("B", name, pname, value), "WARN",
                           "%s.%s = '%s' is not in the illustrative value list held locally. "
                           "Verify against the Space Values sheet of the current mapping "
                           "workbook." % (name, pname, value), pid, pset_name=name)
    findings = groups.emit(ctx, "PSET.006")
    if findings:
        return _mk(ctx, "PSET.006", WARN,
                   "%d controlled-value properties need verification against the mapping "
                   "workbook." % len(findings), findings)
    if controlled == 0:
        library_has_controlled = any(
            p.get("controlled")
            for entry in ctx.lib.sgpsets.values()
            for p in entry.get("properties", []) or [])
        if not library_has_controlled:
            return _mk(ctx, "PSET.006", UNKNOWN,
                       "The loaded mapping source carries no controlled-value lists, so "
                       "enumeration validity cannot be checked. The Revit-format export "
                       "omits them; they live in the 'Space Values' sheet of the BCA "
                       "industry workbook.")
        return _mk(ctx, "PSET.006", UNKNOWN,
                   "The model carries no property that the mapping marks as "
                   "controlled-value, so there was nothing to check.")
    return _mk(ctx, "PSET.006", PASS, "All %d controlled values match." % controlled)


def _pset_007(ctx):
    findings = []
    for oid, quants in sorted(ctx.quantities.items()):
        for pname, pid in sorted(ctx.psets_of(oid).items()):
            if not pname.upper().startswith("SGPSET_") or "DIMENSION" not in pname.upper():
                continue
            for _propid, prop, _t, value in ctx.pset_props.get(pid, []):
                if prop not in quants or value is None:
                    continue
                try:
                    if abs(float(value) - float(quants[prop])) < 1e-6:
                        findings.append(ctx.finding(
                            "PSET.007", "WARN",
                            "%s.%s duplicates the element quantity of the same name."
                            % (pname, prop), ctx.f.get(oid)))
                except (TypeError, ValueError):
                    continue
    findings = _dedupe(findings)
    if findings:
        return _mk(ctx, "PSET.007", WARN,
                   "%d dimension properties duplicate a native quantity." % len(findings),
                   findings)
    return _mk(ctx, "PSET.007", PASS, "No redundant dimension declarations detected.")


def _pset_008(ctx):
    counts = {}
    for _pid, name in ctx.pset_name.items():
        if not name:
            continue
        if any(name.upper().startswith(p) for p in VENDOR_PSET_ALLOW_PREFIX):
            continue
        counts[name] = counts.get(name, 0) + 1
    if not counts:
        return _mk(ctx, "PSET.008", PASS, "No non-standard property sets in the export.")

    groups = _Groups()
    for pid, name in sorted(ctx.pset_name.items()):
        if name in counts:
            groups.add((name,), "WARN",
                       "'%s' is not a Pset_/SGPset_/Qto_ set (%d instances)."
                       % (name, counts[name]), pid, pset_name=name)
    findings = groups.emit(ctx, "PSET.008")
    return _mk(ctx, "PSET.008", WARN,
               "%d non-regulatory property sets (%d instances) are being exported."
               % (len(counts), sum(counts.values())), findings)


# -- identity ---------------------------------------------------------------

def _id_001(ctx):
    findings = []
    seen = {}
    checked = 0
    rooted = ctx.element_ids + ctx.f.of_types(
        ("IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCSPACE"))
    for ent in rooted:
        g = ctx.guid(ent)
        checked += 1
        if len(g) != 22 or any(ch not in GUID_ALPHABET for ch in g):
            findings.append(ctx.finding(
                "ID.001", "WARN", "GlobalId '%s' is not a valid 22-character IFC GUID." % g, ent))
        elif g in seen:
            findings.append(ctx.finding(
                "ID.001", "WARN", "GlobalId '%s' is duplicated." % g, ent))
        else:
            seen[g] = ent.id
    if findings:
        return _mk(ctx, "ID.001", WARN,
                   "%d of %d GlobalIds are malformed or duplicated." % (len(findings), checked),
                   findings)
    return _mk(ctx, "ID.001", PASS,
               "All %d GlobalIds are well formed and unique. Stability across exports cannot "
               "be checked from a single file; compare against the previous submission."
               % checked)


def _id_002(ctx):
    total = len(ctx.element_ids)
    if total == 0:
        return _mk(ctx, "ID.002", UNKNOWN, "No elements to check.")
    missing = [ctx.finding("ID.002", "INFO", "Tag is empty; no traceback to the source element.", e)
               for e in ctx.element_ids if not e.value(spf.IDX_TAG)]
    if missing:
        return _mk(ctx, "ID.002", WARN,
                   "%d of %d elements export no Tag." % (len(missing), total), missing)
    sample = ctx.element_ids[0].value(spf.IDX_TAG)
    return _mk(ctx, "ID.002", PASS,
               "All %d elements carry a source Tag (e.g. '%s')." % (total, sample))


# -- units ------------------------------------------------------------------

def _unit_001(ctx):
    assigns = ctx.f.of_type("IFCUNITASSIGNMENT")
    if not assigns:
        return _mk(ctx, "UNIT.001", FAIL, "No IfcUnitAssignment in the file.")
    declared = {}
    for ua in assigns:
        for uid in spf.as_refs(ua.arg(0)):
            u = ctx.f.get(uid)
            if u is None:
                continue
            if u.type == "IFCSIUNIT":
                utype = u.value(1)
                prefix = u.value(2)
                name = u.value(3)
                declared[utype] = ("%s%s" % (prefix or "", name or "")).strip()
            elif u.type == "IFCCONVERSIONBASEDUNIT":
                declared[u.value(1)] = u.value(2) or "conversion-based"
    missing = [k for k in ("LENGTHUNIT", "AREAUNIT", "VOLUMEUNIT") if k not in declared]
    if missing:
        return _mk(ctx, "UNIT.001", FAIL,
                   "IfcUnitAssignment does not declare: %s. Declared: %s"
                   % (", ".join(missing),
                      ", ".join("%s=%s" % kv for kv in sorted(declared.items()))))
    return _mk(ctx, "UNIT.001", PASS,
               "Units declared: LENGTH=%s, AREA=%s, VOLUME=%s. Consistency with the other "
               "discipline models must be confirmed separately."
               % (declared["LENGTHUNIT"], declared["AREAUNIT"], declared["VOLUMEUNIT"]))


# -- hygiene ----------------------------------------------------------------

GEOMETRY_PRIMITIVES = (
    "IFCCARTESIANPOINT", "IFCCARTESIANPOINTLIST3D", "IFCCARTESIANPOINTLIST2D",
    "IFCINDEXEDPOLYGONALFACE", "IFCINDEXEDPOLYGONALFACEWITHVOIDS",
    "IFCPOLYGONALFACESET", "IFCTRIANGULATEDFACESET", "IFCFACE", "IFCPOLYLOOP",
    "IFCFACEOUTERBOUND", "IFCFACEBOUND",
)


def _qc_001(ctx):
    counts = ctx.f.type_counts()
    total = ctx.f.count() or 1
    geom = sum(counts.get(t, 0) for t in GEOMETRY_PRIMITIVES)
    pct = 100.0 * geom / total
    mb = ctx.f.byte_size / (1024.0 * 1024.0)
    summary = ("%.2f MB, %d entities, %d (%.1f%%) are tessellation/geometry primitives."
               % (mb, total, geom, pct))
    if pct >= 50.0:
        return _mk(ctx, "QC.001", WARN,
                   summary + " Over half the file is geometry primitives; review whether "
                             "non-regulated detail can be excluded.")
    return _mk(ctx, "QC.001", PASS, summary)


def _qc_002(ctx):
    if not ctx.f.geometry_available:
        return _mk(
            ctx, "QC.002", NOT_APPLICABLE,
            "Coincident-geometry analysis is disabled in lean mode to keep large "
            "IFC checks isolated from MicroStation memory.")
    buckets = {}
    for ent in ctx.element_ids:
        try:
            lo, hi = ctx.geo.world_box(ent)
        except Exception:
            continue
        key = (ent.type,
               round(lo[0], 3), round(lo[1], 3), round(lo[2], 3),
               round(hi[0], 3), round(hi[1], 3), round(hi[2], 3))
        buckets.setdefault(key, []).append(ent)
    findings = []
    for key, group in sorted(buckets.items(), key=lambda kv: kv[1][0].id):
        if len(group) < 2:
            continue
        for ent in group[1:]:
            findings.append(ctx.finding(
                "QC.002", "WARN",
                "Coincident with %d other %s at the same bounding box."
                % (len(group) - 1, ent.type), ent,
                detail="Duplicate group: %s" % ", ".join("#%d" % g.id for g in group)))
    if findings:
        return _mk(ctx, "QC.002", WARN,
                   "%d elements are coincident duplicates." % len(findings), findings)
    return _mk(ctx, "QC.002", PASS,
               "No coincident duplicate elements among %d checked." % len(ctx.element_ids))


# -- governance -------------------------------------------------------------

def _doc_001(ctx):
    ed = ctx.lib.editions
    detail = ("mapping_edition=%s, cop_edition=%s, source=%s (%d sets, %d declared but "
              "not transcribed), built from %s"
              % (ed["mapping_edition"], ed["cop_edition"], ed["sgpset_source"],
                 ed["sgpset_count"], ed["untranscribed_count"], ed["source_files"]))

    if not ctx.lib.is_complete or ed["mapping_edition"] == "NOT SET":
        return _mk(ctx, "DOC.001", FAIL,
                   "The audit cannot be stamped with an authoritative mapping edition. "
                   + detail + ". Run build_catalogue.py against the current BCA industry "
                              "mapping workbook before relying on property-level results.")

    notes = []
    age = ed["mapping_age_days"]
    if age is not None and age > STALE_MAPPING_DAYS:
        notes.append("the mapping edition is %d days old (%s); the BCA workbook is "
                     "versioned and explicitly work-in-progress, so verify it against "
                     "info.corenet.gov.sg before submission"
                     % (age, ed["mapping_edition"]))
    if not ed.get("authoritative_mapping"):
        notes.append("the bundled catalogue was not built directly from the official BCA "
                     "industry mapping workbook")
    if ed["cop_edition"] == "NOT SET":
        notes.append("no COP edition was recorded at build time")
    if ed.get("superseded"):
        notes.append("COP %s was selected but COP %s is installed and newer; use the "
                     "superseded edition only for projects still assessed under it"
                     % (ed["cop_edition"], ed["latest_cop"]))

    if notes:
        return _mk(ctx, "DOC.001", WARN,
                   "Report stamped, with caveats: " + "; ".join(notes) + ". " + detail)
    return _mk(ctx, "DOC.001", PASS, "Report stamped: " + detail)


# ---------------------------------------------------------------------------

def _ifc_name(upper_type):
    """Library lookups compare case-insensitively, so the raw upper type is enough."""
    return upper_type


def _dedupe(findings):
    seen = set()
    out = []
    for f in findings:
        key = (f.rule_id, f.message, f.ifc_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out
