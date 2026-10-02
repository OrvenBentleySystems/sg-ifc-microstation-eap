"""Wireframe edges from IFC geometry, for previews without MicroStation.

Needs a file parsed with retain_geometry=True. Covers the representations that
authoring tools export for building elements: extrusions of rectangle, circle,
I-shape and arbitrary profiles, faceted breps, triangulated and polygonal face
sets, surface models, mapped items, boolean results (first operand), swept
disks and bounding boxes. Openings are not cut; the picture shows the element's
overall form, which is enough to recognise it.
"""

import math
import re

from . import spf
from .spf import as_float, as_ref, as_refs, mat_apply, mat_multiply, IDENTITY

MAX_LINES = 4000
CIRCLE_SEGMENTS = 24


def _pt(ifc, pid):
    e = ifc.get(pid)
    if e is None or e.type != "IFCCARTESIANPOINT":
        return None
    vals = spf.as_refs_free(e.arg(0))
    return tuple((vals + [0.0, 0.0, 0.0])[:3]) if vals else None


def _vec(ifc, did, default):
    e = ifc.get(did) if did is not None else None
    if e is None or e.type != "IFCDIRECTION":
        return default
    vals = spf.as_refs_free(e.arg(0))
    if not vals:
        return default
    v = tuple((vals + [0.0, 0.0, 0.0])[:3])
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v) if n > 1e-12 else default


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v, default):
    n = math.sqrt(_dot3(v, v))
    return (v[0] / n, v[1] / n, v[2] / n) if n > 1e-12 else default


def _operator_matrix(ifc, oid):
    """IfcCartesianTransformationOperator2D/3D (and NonUniform) as a 3x4 matrix.

    Axes follow IfcBaseAxis: Axis2 keeps its sign, so mirrored instances stay
    mirrored instead of turning into a 180 degree rotation.
    """
    e = ifc.get(oid) if oid is not None else None
    if e is None:
        return IDENTITY
    is_2d = "2D" in e.type
    a1 = _vec(ifc, as_ref(e.arg(0)), None)
    a2 = _vec(ifc, as_ref(e.arg(1)), None)
    origin = _pt(ifc, as_ref(e.arg(2))) or (0.0, 0.0, 0.0)
    scale = as_float(e.arg(3)) if len(e.args) > 3 else None
    scale = 1.0 if scale is None else scale
    s2 = s3 = scale
    if is_2d:
        z = (0.0, 0.0, 1.0)
        if e.type.endswith("NONUNIFORM") and len(e.args) > 4:
            v = as_float(e.arg(4))
            s2 = scale if v is None else v
    else:
        z = _vec(ifc, as_ref(e.arg(4)), (0.0, 0.0, 1.0)) if len(e.args) > 4 \
            else (0.0, 0.0, 1.0)
        if e.type.endswith("NONUNIFORM"):
            v2 = as_float(e.arg(5)) if len(e.args) > 5 else None
            v3 = as_float(e.arg(6)) if len(e.args) > 6 else None
            s2 = scale if v2 is None else v2
            s3 = scale if v3 is None else v3
    x0 = a1 if a1 is not None else ((1.0, 0.0, 0.0) if abs(z[0]) < 0.9 else (0.0, 1.0, 0.0))
    x = _unit(_sub(x0, tuple(c * _dot3(x0, z) for c in z)), (1.0, 0.0, 0.0))
    if a2 is not None:
        y0 = a2
        y0 = _sub(y0, tuple(c * _dot3(y0, z) for c in z))
        y = _unit(_sub(y0, tuple(c * _dot3(y0, x) for c in x)), None)
    else:
        y = None
    if y is None:
        y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
    return (x[0] * scale, y[0] * s2, z[0] * s3, origin[0],
            x[1] * scale, y[1] * s2, z[1] * s3, origin[1],
            x[2] * scale, y[2] * s2, z[2] * s3, origin[2])


class _Edges(object):
    def __init__(self, ifc):
        self.f = ifc
        self.geo = spf.Geometry(ifc)
        self.lines = []

    def add(self, m, pts, closed=False):
        if len(self.lines) >= MAX_LINES or len(pts) < 2:
            return
        world = [mat_apply(m, p) for p in pts]
        if closed and world[0] != world[-1]:
            world.append(world[0])
        self.lines.append(world)

    # -- curves and profiles ------------------------------------------------

    def curve_points(self, cid, depth=0):
        """Points of a 2D/3D bounded curve, or [] when unsupported."""
        e = self.f.get(cid) if cid is not None else None
        if e is None or depth > 6:
            return []
        t = e.type
        if t == "IFCPOLYLINE":
            return [p for p in (_pt(self.f, i) for i in as_refs(e.arg(0))) if p]
        if t == "IFCINDEXEDPOLYCURVE":
            plist = self.f.get(as_ref(e.arg(0)))
            coords = [tuple((c + [0.0, 0.0, 0.0])[:3])
                      for c in spf._nested_floats(plist.arg(0))] if plist is not None else []
            segs = e.arg(1).strip() if len(e.args) > 1 else "$"
            if segs in ("$", "*", ""):
                return coords
            order = []
            for v in re.findall(r"\d+", re.sub(r"IFC[A-Z]+INDEX", "", segs.upper())):
                i = int(v)
                if not order or order[-1] != i:
                    order.append(i)
            return [coords[i - 1] for i in order if 0 < i <= len(coords)]
        if t == "IFCCOMPOSITECURVE":
            out = []
            for sid in as_refs(e.arg(0)):
                seg = self.f.get(sid)
                if seg is None:
                    continue
                pts = self.curve_points(as_ref(seg.arg(2)), depth + 1)
                if (seg.value(1) or "").upper() in ("F", ".F.", "FALSE"):
                    pts = list(reversed(pts))
                out.extend(pts)
            return out
        if t == "IFCTRIMMEDCURVE":
            arc = self.trimmed_arc(e)
            return arc if arc is not None else self.curve_points(as_ref(e.arg(0)), depth + 1)
        if t == "IFCCIRCLE":
            m = self.geo.axis_matrix(as_ref(e.arg(0)))
            r = as_float(e.arg(1)) or 0.0
            return [mat_apply(m, (r * math.cos(a), r * math.sin(a), 0.0)) for a in
                    (2 * math.pi * k / CIRCLE_SEGMENTS for k in range(CIRCLE_SEGMENTS + 1))]
        return []

    def angle_scale(self):
        """Radians per plane-angle unit of the file (degrees when declared)."""
        cached = getattr(self, "_angle_scale", None)
        if cached is not None:
            return cached
        scale = 1.0
        for unit in self.f.of_type("IFCCONVERSIONBASEDUNIT"):
            if (unit.value(1) or "").upper() == "PLANEANGLEUNIT":
                mwu = self.f.get(as_ref(unit.arg(3)))
                if mwu is not None:
                    _t, val = spf.typed_value(mwu.arg(0))
                    try:
                        scale = float(val)
                    except (TypeError, ValueError):
                        scale = math.pi / 180.0
                break
        self._angle_scale = scale
        return scale

    def trimmed_arc(self, e):
        """Points of an IfcTrimmedCurve on an IfcCircle, or None."""
        basis = self.f.get(as_ref(e.arg(0)))
        if basis is None or basis.type != "IFCCIRCLE":
            return None
        m = self.geo.axis_matrix(as_ref(basis.arg(0)))
        r = as_float(basis.arg(1)) or 0.0
        if r <= 0.0:
            return None

        def angle(trim_tok):
            text = trim_tok or ""
            match = re.search(r"IFCPARAMETERVALUE\(\s*([-+0-9.Ee]+)\s*\)", text.upper())
            if match:
                return float(match.group(1)) * self.angle_scale()
            for ref in re.findall(r"#(\d+)", text):
                p = _pt(self.f, int(ref))
                if p is None:
                    continue
                d = _sub(p, (m[3], m[7], m[11]))
                lx = m[0] * d[0] + m[4] * d[1] + m[8] * d[2]
                ly = m[1] * d[0] + m[5] * d[1] + m[9] * d[2]
                return math.atan2(ly, lx)
            return None

        a1, a2 = angle(e.arg(1)), angle(e.arg(2))
        if a1 is None or a2 is None:
            return None
        sense = (e.value(3) or "T").upper() not in ("F", ".F.", "FALSE")
        sweep = (a2 - a1) if sense else (a1 - a2)
        sweep %= 2 * math.pi
        if sweep < 1e-9:
            sweep = 2 * math.pi
        steps = max(2, int(CIRCLE_SEGMENTS * sweep / (2 * math.pi)) + 1)
        sign = 1.0 if sense else -1.0
        return [mat_apply(m, (r * math.cos(a1 + sign * sweep * k / steps),
                              r * math.sin(a1 + sign * sweep * k / steps), 0.0))
                for k in range(steps + 1)]

    def profile(self, pid):
        """Loops of a profile, in the profile's own 2D frame."""
        e = self.f.get(pid) if pid is not None else None
        if e is None:
            return []
        t = e.type
        if t in ("IFCARBITRARYCLOSEDPROFILEDEF", "IFCARBITRARYPROFILEDEFWITHVOIDS"):
            loops = [self.curve_points(as_ref(e.arg(2)))]
            if t == "IFCARBITRARYPROFILEDEFWITHVOIDS" and len(e.args) > 3:
                loops.extend(self.curve_points(i) for i in as_refs(e.arg(3)))
            return [loop for loop in loops if len(loop) >= 2]
        if t == "IFCARBITRARYOPENPROFILEDEF":
            pts = self.curve_points(as_ref(e.arg(2)))
            return [pts] if len(pts) >= 2 else []
        m = self.geo.axis_matrix(as_ref(e.arg(2))) if len(e.args) > 2 else IDENTITY
        if t in ("IFCRECTANGLEPROFILEDEF", "IFCROUNDEDRECTANGLEPROFILEDEF",
                 "IFCRECTANGLEHOLLOWPROFILEDEF", "IFCISHAPEPROFILEDEF",
                 "IFCTSHAPEPROFILEDEF", "IFCLSHAPEPROFILEDEF", "IFCUSHAPEPROFILEDEF",
                 "IFCCSHAPEPROFILEDEF", "IFCZSHAPEPROFILEDEF",
                 "IFCASYMMETRICISHAPEPROFILEDEF"):
            x = as_float(e.arg(3)) or 0.0
            y = as_float(e.arg(4)) or x
            if t in ("IFCLSHAPEPROFILEDEF", "IFCTSHAPEPROFILEDEF", "IFCUSHAPEPROFILEDEF",
                     "IFCCSHAPEPROFILEDEF", "IFCZSHAPEPROFILEDEF"):
                # These profiles give Depth (along Y) before Width (along X).
                x, y = y, x
            hx, hy = x / 2.0, y / 2.0
            return [[mat_apply(m, p) for p in ((-hx, -hy, 0.0), (hx, -hy, 0.0),
                                                (hx, hy, 0.0), (-hx, hy, 0.0),
                                                (-hx, -hy, 0.0))]]
        if t in ("IFCCIRCLEPROFILEDEF", "IFCCIRCLEHOLLOWPROFILEDEF", "IFCELLIPSEPROFILEDEF"):
            r = as_float(e.arg(3)) or 0.0
            r2 = as_float(e.arg(4)) if t == "IFCELLIPSEPROFILEDEF" else r
            r2 = r if r2 is None else r2
            return [[mat_apply(m, (r * math.cos(a), r2 * math.sin(a), 0.0)) for a in
                     (2 * math.pi * k / CIRCLE_SEGMENTS for k in range(CIRCLE_SEGMENTS + 1))]]
        if t == "IFCDERIVEDPROFILEDEF":
            op = _operator_matrix(self.f, as_ref(e.arg(3)))
            return [[mat_apply(op, p) for p in loop]
                    for loop in self.profile(as_ref(e.arg(2)))]
        return []

    # -- solids and surfaces --------------------------------------------------

    def item(self, iid, m, depth=0):
        e = self.f.get(iid) if iid is not None else None
        if e is None or depth > 8 or len(self.lines) >= MAX_LINES:
            return
        t = e.type
        if t in ("IFCEXTRUDEDAREASOLID", "IFCEXTRUDEDAREASOLIDTAPERED"):
            local = mat_multiply(m, self.geo.axis_matrix(as_ref(e.arg(1))))
            d = _vec(self.f, as_ref(e.arg(2)), (0.0, 0.0, 1.0))
            length = as_float(e.arg(3)) or 0.0
            off = (d[0] * length, d[1] * length, d[2] * length)
            for loop in self.profile(as_ref(e.arg(0))):
                top = [(p[0] + off[0], p[1] + off[1], p[2] + off[2]) for p in loop]
                self.add(local, loop)
                self.add(local, top)
                step = max(1, len(loop) // 24)
                for p, q in list(zip(loop, top))[::step]:
                    self.add(local, [p, q])
        elif t in ("IFCFACETEDBREP", "IFCFACETEDBREPWITHVOIDS", "IFCMANIFOLDSOLIDBREP"):
            self.shell(as_ref(e.arg(0)), m)
        elif t in ("IFCCLOSEDSHELL", "IFCOPENSHELL", "IFCCONNECTEDFACESET"):
            self.shell(iid, m)
        elif t in ("IFCSHELLBASEDSURFACEMODEL", "IFCFACEBASEDSURFACEMODEL"):
            for sid in as_refs(e.arg(0)):
                self.shell(sid, m)
        elif t == "IFCTRIANGULATEDFACESET":
            self.indexed(m, as_ref(e.arg(0)), e.arg(3), triangles=True)
        elif t == "IFCPOLYGONALFACESET":
            self.indexed(m, as_ref(e.arg(0)), e.arg(2), triangles=False)
        elif t == "IFCMAPPEDITEM":
            rmap = self.f.get(as_ref(e.arg(0)))
            if rmap is None:
                return
            origin = self.geo.axis_matrix(as_ref(rmap.arg(0)))
            target = _operator_matrix(self.f, as_ref(e.arg(1)))
            sub = mat_multiply(m, mat_multiply(target, origin))
            rep = self.f.get(as_ref(rmap.arg(1)))
            if rep is not None:
                for it in as_refs(rep.arg(3)):
                    self.item(it, sub, depth + 1)
        elif t in ("IFCBOOLEANRESULT", "IFCBOOLEANCLIPPINGRESULT"):
            self.item(as_ref(e.arg(1)), m, depth + 1)
        elif t == "IFCSWEPTDISKSOLID":
            self.add(m, self.curve_points(as_ref(e.arg(0))))
        elif t == "IFCBOUNDINGBOX":
            c = _pt(self.f, as_ref(e.arg(0))) or (0.0, 0.0, 0.0)
            self.box(m, c, (c[0] + (as_float(e.arg(1)) or 0.0),
                            c[1] + (as_float(e.arg(2)) or 0.0),
                            c[2] + (as_float(e.arg(3)) or 0.0)))
        elif t in ("IFCPOLYLINE", "IFCINDEXEDPOLYCURVE", "IFCCOMPOSITECURVE",
                   "IFCTRIMMEDCURVE", "IFCCIRCLE"):
            self.add(m, self.curve_points(iid))
        elif t in ("IFCGEOMETRICSET", "IFCGEOMETRICCURVESET"):
            for it in as_refs(e.arg(0)):
                self.item(it, m, depth + 1)

    def shell(self, sid, m):
        shell = self.f.get(sid) if sid is not None else None
        if shell is None:
            return
        for fid in as_refs(shell.arg(0)):
            face = self.f.get(fid)
            if face is None:
                continue
            for bid in as_refs(face.arg(0)):
                bound = self.f.get(bid)
                loop = self.f.get(as_ref(bound.arg(0))) if bound is not None else None
                if loop is not None and loop.type == "IFCPOLYLOOP":
                    pts = [p for p in (_pt(self.f, i) for i in as_refs(loop.arg(0))) if p]
                    self.add(m, pts, closed=True)
            if len(self.lines) >= MAX_LINES:
                return

    def indexed(self, m, coord_id, index_tok, triangles):
        plist = self.f.get(coord_id)
        if plist is None:
            return
        coords = [tuple((c + [0.0, 0.0, 0.0])[:3]) for c in spf._nested_floats(plist.arg(0))]
        if triangles:
            faces = [[int(v) for v in f] for f in spf._nested_floats(index_tok)]
        else:
            faces = []
            for fid in as_refs(index_tok):
                fe = self.f.get(fid)
                if fe is not None:
                    faces.append([int(v) for v in spf.as_refs_free(fe.arg(0))])
        seen = set()
        for face in faces:
            for a, b in zip(face, face[1:] + face[:1]):
                key = (min(a, b), max(a, b))
                if key in seen or not (0 < a <= len(coords) and 0 < b <= len(coords)):
                    continue
                seen.add(key)
                self.add(m, [coords[a - 1], coords[b - 1]])
                if len(self.lines) >= MAX_LINES:
                    return

    def box(self, m, lo, hi):
        x0, y0, z0 = lo
        x1, y1, z1 = hi
        for loop in (((x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)),
                     ((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1))):
            self.add(m, list(loop), closed=True)
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            self.add(m, [(x, y, z0), (x, y, z1)])

    # -- products -------------------------------------------------------------

    def product(self, product):
        m = self.geo.placement_matrix(as_ref(product.arg(spf.IDX_PLACEMENT)))
        rep = self.f.get(as_ref(product.arg(spf.IDX_REPRESENTATION)))
        if rep is None or rep.type != "IFCPRODUCTDEFINITIONSHAPE":
            return
        reps = [self.f.get(r) for r in as_refs(rep.arg(2))]
        reps = [r for r in reps if r is not None and r.type == "IFCSHAPEREPRESENTATION"]
        body = [r for r in reps if (r.value(1) or "").upper() == "BODY"]
        others = [r for r in reps if (r.value(1) or "").upper() not in
                  ("AXIS", "FOOTPRINT", "ANNOTATION", "PROFILE")]
        for r in body or others[:1]:
            for it in as_refs(r.arg(3)):
                self.item(it, m)


def wireframe(ifc, product, parts=(), unit_scale=1.0):
    """Edges of an IFC product and its aggregated parts, in metres.

    Returns [] when geometry was not retained or nothing could be drawn.
    """
    if ifc is None or product is None or not ifc.geometry_available:
        return []
    edges = _Edges(ifc)
    for item in [product] + [p for p in parts if p is not None]:
        try:
            edges.product(item)
        except Exception:
            continue
        if len(edges.lines) >= MAX_LINES:
            break
    s = unit_scale
    return [[(p[0] * s, p[1] * s, p[2] * s) for p in line] for line in edges.lines]
