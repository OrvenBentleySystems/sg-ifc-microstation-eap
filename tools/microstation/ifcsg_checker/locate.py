"""Bridge from an IFC entity to geometry in the MicroStation session.

An .ifc attachment may expose IFC4 EC identity, depending on the MicroStation
build and import path. GlobalId is used when available. Otherwise the link is
spatial matching is available only when the parser explicitly retains geometry.
The normal checker uses lean mode and exact GlobalId matching only.

    IFC coordinate -> metres -> reference UOR
"""

from . import spf

SI_PREFIX = {
    "EXA": 1e18, "PETA": 1e15, "TERA": 1e12, "GIGA": 1e9, "MEGA": 1e6,
    "KILO": 1e3, "HECTO": 1e2, "DECA": 1e1, "DECI": 1e-1, "CENTI": 1e-2,
    "MILLI": 1e-3, "MICRO": 1e-6, "NANO": 1e-9, "PICO": 1e-12,
    "FEMTO": 1e-15, "ATTO": 1e-18,
}

MAX_RANGE_INDEX_ELEMENTS = 100000
MAX_GUID_SCAN_ELEMENTS = 50000
MAX_SELECTION_ELEMENTS = 500

try:
    from MSPyBentleyGeom import DRange3d
    from MSPyDgnPlatform import (ElementHandle, ChildElemIter, ExposeChildrenReason,
                                 IViewManager, DgnECManager, FindInstancesScope,
                                 FindInstancesScopeOption)
    from MSPyDgnView import SelectionSetManager
    from MSPyECObjects import DgnECHostType, ECQuery, ECQueryProcessFlags, ECValue
    IN_MICROSTATION = True
except ImportError:
    IN_MICROSTATION = False


def ifc_length_to_metres(ifc):
    """Scale that converts a length in the file's own unit into metres."""
    for ua in ifc.of_type("IFCUNITASSIGNMENT"):
        for uid in spf.as_refs(ua.arg(0)):
            u = ifc.get(uid)
            if u is None:
                continue
            if u.type == "IFCSIUNIT" and (u.value(1) or "").upper() == "LENGTHUNIT":
                prefix = (u.value(2) or "").upper()
                return SI_PREFIX.get(prefix, 1.0)
            if u.type == "IFCCONVERSIONBASEDUNIT" and (u.value(1) or "").upper() == "LENGTHUNIT":
                mwu = ifc.get(spf.as_ref(u.arg(3)))
                if mwu is not None and mwu.type == "IFCMEASUREWITHUNIT":
                    _t, val = spf.typed_value(mwu.arg(0))
                    base = ifc.get(spf.as_ref(mwu.arg(1)))
                    scale = 1.0
                    if base is not None and base.type == "IFCSIUNIT":
                        scale = SI_PREFIX.get((base.value(2) or "").upper(), 1.0)
                    try:
                        return float(val) * scale
                    except (TypeError, ValueError):
                        return 1.0
    return 1.0


class Locator(object):
    """Select DGN geometry corresponding to IFC entities."""

    def __init__(self, source, ifc, geometry):
        self.source = source
        self.f = ifc
        self.geo = geometry
        self.available = IN_MICROSTATION and source.is_locatable
        self.reason = "" if self.available else "No live geometry for this source."
        self.metre_scale = ifc_length_to_metres(ifc)
        self._model = None
        self._model_ref = None
        self._uor_per_metre = None
        self._index = None
        self._guids = None
        self.guid_hits = 0
        self.geometry_hits = 0

        self.index_truncated = False
        self.guid_index_truncated = False
        self.index_errors = 0
        self.guid_errors = 0
        if self.available:
            try:
                self._prepare()
            except Exception as exc:
                self.available = False
                self.reason = "Locator setup failed: %s" % exc

    # -- setup --------------------------------------------------------------

    def _prepare(self):
        att = self.source.attachment
        self._model_ref = att if att is not None else self.source.model_ref
        self._model = self._model_ref.GetDgnModel()
        if self._model is None:
            raise RuntimeError("MicroStation returned no DGN model.")
        if self.f.geometry_available:
            info = self._model.GetModelInfo()
            if info is None:
                raise RuntimeError("MicroStation returned no model information.")
            self._uor_per_metre = float(info.GetUorPerMeter())

    def _ensure_index(self):
        """Build (once) a list of (elementRef, DRange3d) for the target model."""
        if self._index is not None:
            return self._index
        index = []
        for ref in self._model.GetGraphicElements():
            if len(index) >= MAX_RANGE_INDEX_ELEMENTS:
                self.index_truncated = True
                break
            eh = ElementHandle(ref, self._model)
            self._collect(eh, ref, index, 0)
        self._index = index
        return index

    def _collect(self, eh, ref, index, depth):
        if len(index) >= MAX_RANGE_INDEX_ELEMENTS:
            self.index_truncated = True
            return
        rng = DRange3d()
        try:
            handler = eh.GetDisplayHandler()
            if handler is not None:
                handler.CalcElementRange(eh, rng, None)
                index.append((ref, (rng.low.x, rng.low.y, rng.low.z,
                                    rng.high.x, rng.high.y, rng.high.z)))
        except Exception:
            self.index_errors += 1
        if depth >= 3:
            return
        try:
            child = ChildElemIter(eh, ExposeChildrenReason.eQuery)
            while child.IsValid():
                self._collect(child, child.GetElementRef(), index, depth + 1)
                child = child.ToNext()
        except Exception:
            self.index_errors += 1

    # -- coordinate conversion ---------------------------------------------

    def to_uor(self, point):
        s = self.metre_scale * self._uor_per_metre
        return (point[0] * s, point[1] * s, point[2] * s)

    def entity_box_uor(self, ifc_id):
        ent = self.f.get(ifc_id)
        if ent is None:
            return None
        try:
            lo, hi = self.geo.world_box(ent)
        except Exception:
            return None
        lo_u = self.to_uor(lo)
        hi_u = self.to_uor(hi)
        return (min(lo_u[0], hi_u[0]), min(lo_u[1], hi_u[1]), min(lo_u[2], hi_u[2]),
                max(lo_u[0], hi_u[0]), max(lo_u[1], hi_u[1]), max(lo_u[2], hi_u[2]))

    # -- actions ------------------------------------------------------------

    def _ensure_guid_index(self):
        """IFC GlobalId -> element refs.

        MicroStation surfaces the IFC4 EC schema on an attached IFC, and every
        instance carries the file's own GlobalId. Matching on that key is exact,
        so it beats any spatial heuristic when the data is there.
        """
        if self._guids is not None:
            return self._guids
        index = {}
        try:
            mgr = DgnECManager.GetManager()
            if mgr is None:
                self._guids = index
                return index
            opts = FindInstancesScopeOption(DgnECHostType.eElement)
            flags = ECQueryProcessFlags.eECQUERY_PROCESS_SearchAllClasses
            scanned = 0
            for ref in self._model.GetGraphicElements():
                if scanned >= MAX_GUID_SCAN_ELEMENTS:
                    self.guid_index_truncated = True
                    break
                scanned += 1
                eh = ElementHandle(ref, self._model)
                try:
                    found = mgr.FindInstances(FindInstancesScope.CreateScope(eh, opts),
                                              ECQuery.CreateQuery(flags))
                except Exception:
                    self.guid_errors += 1
                    continue
                iterable = found[0] if isinstance(found, tuple) else found
                for inst in iterable:
                    # The intrinsic DgnElementSchema instance comes first and has no
                    # GlobalId, so the IFC4 schema has to be named explicitly.
                    try:
                        cls = inst.GetClass()
                        if not str(cls.GetSchema().GetName()).startswith("IFC4"):
                            continue
                        value = ECValue()
                        inst.GetValue(value, "GlobalId")
                        guid = str(value.ToString())
                    except Exception:
                        self.guid_errors += 1
                        continue
                    if guid and guid != "<null>":
                        index.setdefault(guid, []).append(ref)
                        break
        except Exception:
            index = {}
            self.guid_errors += 1
        self._guids = index
        return index

    def guid_coverage(self):
        """How many DGN elements carry an IFC GlobalId."""
        if not self.available:
            return 0
        return len(self._ensure_guid_index())

    def matches(self, ifc_id, pad_uor=1000.0):
        """Reference-model elements that represent this IFC entity.

        Prefers the entity's GlobalId, which is an exact key. Falls back to the
        spatial test when the DGN carries no EC identity for it.

        Adjacent elements routinely overlap, so a plain overlap test over-selects.
        Elements whose whole range sits inside the IFC box are treated as the real
        members; if none do, the single closest overlapping element is returned.
        """
        entity = self.f.get(ifc_id)
        guid = entity.value(spf.IDX_GLOBALID) if entity is not None else None
        if guid:
            refs = self._ensure_guid_index().get(guid)
            if refs:
                self.guid_hits += 1
                return list(refs)

        if not self.f.geometry_available:
            return []
        box = self.entity_box_uor(ifc_id)
        if box is None:
            return []
        lx, ly, lz, hx, hy, hz = box
        plx, ply, plz = lx - pad_uor, ly - pad_uor, lz - pad_uor
        phx, phy, phz = hx + pad_uor, hy + pad_uor, hz + pad_uor

        contained = []
        overlapping = []
        for ref, r in self._ensure_index():
            if r[0] > phx or r[3] < plx:
                continue
            if r[1] > phy or r[4] < ply:
                continue
            if r[2] > phz or r[5] < plz:
                continue
            if (r[0] >= plx and r[1] >= ply and r[2] >= plz
                    and r[3] <= phx and r[4] <= phy and r[5] <= phz):
                contained.append((ref, r))
            else:
                overlapping.append((ref, r))

        if contained:
            self.geometry_hits += 1
            return [c[0] for c in contained]
        if not overlapping:
            return []
        cx, cy, cz = (lx + hx) * 0.5, (ly + hy) * 0.5, (lz + hz) * 0.5

        def distance(item):
            r = item[1]
            mx, my, mz = (r[0] + r[3]) * 0.5, (r[1] + r[4]) * 0.5, (r[2] + r[5]) * 0.5
            return (mx - cx) ** 2 + (my - cy) ** 2 + (mz - cz) ** 2

        overlapping.sort(key=distance)
        self.geometry_hits += 1
        return [overlapping[0][0]]

    def select(self, ifc_ids, add=False, max_elements=MAX_SELECTION_ELEMENTS):
        """Put the geometry for these IFC entities into the selection set."""
        if not self.available:
            return (0, self.reason)
        ifc_ids = list(ifc_ids)
        ssm = SelectionSetManager.GetManager()
        if ssm is None:
            return (0, "MicroStation selection manager is unavailable.")
        if not add:
            try:
                ssm.EmptyAll()
            except Exception as exc:
                return (0, "Could not clear the current selection: %s" % exc)
        placed = 0
        missed = 0
        for ifc_id in ifc_ids:
            if placed >= max_elements:
                break
            refs = self.matches(ifc_id)
            if not refs:
                missed += 1
                continue
            for ref in refs[:max_elements - placed]:
                try:
                    ssm.AddElement(ref, self._model_ref)
                    placed += 1
                except Exception:
                    pass
        if placed == 0:
            return (0, "No geometry found for %d entity/entities." % len(ifc_ids))
        _repaint()
        note = "Selected %d element(s)." % placed
        if missed:
            note += " %d entity/entities had no geometry match." % missed
        if placed >= max_elements:
            note += " Selection capped at %d elements for session stability." % max_elements
        if self.guid_index_truncated or self.index_truncated:
            note += " The locator safety limit was reached; some matches may be unavailable."
        return (placed, note)

    def isolate(self, ifc_ids):
        """Select these entities and show only them, via MicroStation's display set."""
        if not self.available:
            return (0, self.reason)
        placed, note = self.select(ifc_ids)
        if placed == 0:
            return (0, note)
        try:
            from MSPyMstnPlatform import PyCadInputQueue
            PyCadInputQueue.SendKeyin("displayset set")
            _repaint()
            return (placed, "Isolated %d element(s). Use 'Show all' to restore." % placed)
        except Exception as exc:
            return (placed, "Selected %d, but isolate failed: %s" % (placed, exc))

    @staticmethod
    def show_all():
        """Clear the display set so the whole model is visible again."""
        try:
            from MSPyMstnPlatform import PyCadInputQueue
            PyCadInputQueue.SendKeyin("displayset clear")
            _repaint()
            return (True, "Display set cleared.")
        except Exception as exc:
            return (False, "Could not clear the display set: %s" % exc)

    def close(self):
        """Release cached native references and large lookup tables."""
        self._index = None
        self._guids = None
        self._model = None
        self._model_ref = None
        self.available = False


def _repaint():
    """Tk owns the message loop while the window is open, so ask for a repaint."""
    vp = _active_viewport()
    if vp is None:
        return
    for name in ("SynchWithViewInfo",):
        if hasattr(vp, name):
            try:
                getattr(vp, name)(False, False)
                return
            except Exception:
                pass


def _active_viewport():
    if not IN_MICROSTATION:
        return None
    try:
        vs = IViewManager.GetActiveViewSet()
    except Exception:
        return None
    for name in ("GetSelectedViewport", "GetCurrentViewport", "GetFirstViewport"):
        if hasattr(vs, name):
            try:
                vp = getattr(vs, name)()
                if vp is not None:
                    return vp
            except Exception:
                continue
    for idx in range(8):
        try:
            vp = vs.GetViewport(idx)
            if vp is not None:
                return vp
        except Exception:
            continue
    return None
