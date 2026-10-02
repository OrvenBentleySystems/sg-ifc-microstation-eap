"""Wireframe pictures of a single object, for the preview pane and the report.

A wireframe is a list of polylines in model coordinates (metres). It is
projected to an isometric view and drawn with depth shading: near edges dark,
far edges light. The same projection feeds the Tk canvas and an SVG string, so
the window and the HTML report show the same picture. Standard library only.
"""

import math

MAX_SEGMENTS = 6000
NEAR = (27, 35, 48)
FAR = (184, 194, 208)

# Isometric-like view from the south-west, looking down 30 degrees.
_YAW = math.radians(-45.0)
_PITCH = math.radians(30.0)


def _view_axes():
    cy, sy = math.cos(_YAW), math.sin(_YAW)
    cp, sp = math.cos(_PITCH), math.sin(_PITCH)
    right = (cy, sy, 0.0)
    up = (-sy * sp, cy * sp, cp)
    toward = (sy * cp, -cy * cp, sp)
    return right, up, toward


_RIGHT, _UP, _TOWARD = _view_axes()


def segments(wire, limit=MAX_SEGMENTS):
    """Flatten polylines to ((x,y,z),(x,y,z)) segments, capped for speed."""
    out = []
    for line in wire or []:
        for a, b in zip(line, line[1:]):
            if a != b:
                out.append((a, b))
    if len(out) > limit:
        step = len(out) / float(limit)
        out = [out[int(i * step)] for i in range(limit)]
    return out


def bounds(wire):
    pts = [p for line in wire or [] for p in line]
    if not pts:
        return None
    return (tuple(min(p[i] for p in pts) for i in range(3)),
            tuple(max(p[i] for p in pts) for i in range(3)))


def dimensions_text(wire):
    box = bounds(wire)
    if box is None:
        return ""
    lo, hi = box
    return "%.2f x %.2f x %.2f m" % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2])


def project(wire, width, height, margin=14, limit=MAX_SEGMENTS):
    """Return [(x1, y1, x2, y2, shade 0..1)] in pixel space, far edges first."""
    segs = segments(wire, limit)
    if not segs:
        return []

    def view(p):
        return (p[0] * _RIGHT[0] + p[1] * _RIGHT[1] + p[2] * _RIGHT[2],
                p[0] * _UP[0] + p[1] * _UP[1] + p[2] * _UP[2],
                p[0] * _TOWARD[0] + p[1] * _TOWARD[1] + p[2] * _TOWARD[2])

    pv = [(view(a), view(b)) for a, b in segs]
    xs = [c[0] for s in pv for c in s]
    ys = [c[1] for s in pv for c in s]
    ds = [c[2] for s in pv for c in s]
    w = max(max(xs) - min(xs), 1e-9)
    h = max(max(ys) - min(ys), 1e-9)
    scale = min((width - 2 * margin) / w, (height - 2 * margin) / h)
    ox = (width - w * scale) / 2.0 - min(xs) * scale
    oy = (height + h * scale) / 2.0 + min(ys) * scale
    dmin, dmax = min(ds), max(ds)
    dspan = max(dmax - dmin, 1e-9)
    out = []
    for a, b in pv:
        shade = 1.0 - (((a[2] + b[2]) * 0.5) - dmin) / dspan
        out.append((ox + a[0] * scale, oy - a[1] * scale,
                    ox + b[0] * scale, oy - b[1] * scale, shade))
    out.sort(key=lambda s: -s[4])
    return out


def colour(shade):
    t = max(0.0, min(1.0, shade))
    return "#%02x%02x%02x" % tuple(int(NEAR[i] + (FAR[i] - NEAR[i]) * t) for i in range(3))


def _esc(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def svg(wire, width=320, height=220, caption="", limit=MAX_SEGMENTS):
    """Inline SVG of the wireframe, or None when there is nothing to draw."""
    lines = project(wire, width, height, limit=limit)
    if not lines:
        return None
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
             'viewBox="0 0 %d %d" role="img"><title>%s</title>'
             '<rect width="100%%" height="100%%" fill="#ffffff"/>'
             % (width, height, width, height, _esc(caption or "object preview"))]
    for x1, y1, x2, y2, shade in lines:
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="%.1f"/>'
                     % (x1, y1, x2, y2, colour(shade), 1.4 if shade < 0.5 else 0.9))
    dims = dimensions_text(wire)
    if dims:
        parts.append('<text x="8" y="%d" font-family="Segoe UI,Arial" font-size="11" '
                     'fill="#667085">%s</text>' % (height - 8, _esc(dims)))
    parts.append("</svg>")
    return "".join(parts)


def draw(canvas, wire, width, height, empty_text="No picture available."):
    """Draw on a Tk canvas. Returns True when something was drawn."""
    canvas.delete("all")
    lines = project(wire, width, height)
    if not lines:
        canvas.create_text(width / 2, height / 2, text=empty_text, fill="#667085",
                           width=width - 40, justify="center", font=("Segoe UI", 9))
        return False
    for x1, y1, x2, y2, shade in lines:
        canvas.create_line(x1, y1, x2, y2, fill=colour(shade),
                           width=2 if shade < 0.5 else 1)
    dims = dimensions_text(wire)
    if dims:
        canvas.create_text(8, height - 8, text=dims, anchor="sw", fill="#667085",
                           font=("Segoe UI", 8))
    return True


# -- picture sources ---------------------------------------------------------

def _parts(report, oid):
    """Aggregated parts of an object (curtain wall panels, stair flights)."""
    ctx = report.ctx
    index = getattr(ctx, "_children_index", None)
    if index is None:
        index = {}
        for child, parent in ctx.aggregate_parent.items():
            index.setdefault(parent, []).append(child)
        ctx._children_index = index
    return [report.f.get(c) for c in index.get(oid, [])]


def object_wire(report, oid, locator=None):
    """(edges in metres, note) for one object.

    MicroStation's own geometry is used when the object is in a live reference;
    otherwise the IFC geometry, when the file was read with geometry retained.
    """
    note = ""
    if locator is not None and getattr(locator, "available", False):
        lines, note = locator.wireframe(oid)
        if lines:
            return lines, ""
        # MicroStation merges aggregate parts (roof slabs, stair flights,
        # curtain-wall panels) into their parent, so draw the parent instead.
        parent = report.ctx.aggregate_parent.get(oid)
        parent_ent = report.f.get(parent) if parent is not None else None
        if parent_ent is not None and parent_ent.type not in (
                "IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCSPACE"):
            lines, _parent_note = locator.wireframe(parent)
            if lines:
                return lines, ("Shown with its parent %s #%s; MicroStation merges the "
                               "parts of an aggregate." % (parent_ent.type, parent))
    ent = report.f.get(oid)
    if ent is not None and report.f.geometry_available:
        from . import ifcgeom
        from .locate import ifc_length_to_metres
        scale = getattr(report, "_metre_scale", None)
        if scale is None:
            scale = report._metre_scale = ifc_length_to_metres(report.f)
        lines = ifcgeom.wireframe(report.f, ent, _parts(report, oid), unit_scale=scale)
        if lines:
            return lines, ""
        note = note or "This object has no drawable IFC geometry."
    elif not note:
        note = ("No picture: open the IFC as a MicroStation reference, or check a file "
                "small enough for the checker to keep its geometry.")
    return [], note


def collect_pictures(report, locator=None, limit=None, progress=None, cancel=None,
                     width=240, height=170):
    """{ifc id: svg} for the report's first objects to fix."""
    from .report import MAX_PICTURES, REPORT_SEGMENTS
    targets = report.picture_targets(limit or MAX_PICTURES)
    out = {}
    for index, oid in enumerate(targets):
        if cancel is not None and cancel():
            break
        if progress is not None:
            progress("Drawing object %d of %d" % (index + 1, len(targets)),
                     index, len(targets))
        lines, _note = object_wire(report, oid, locator)
        if lines:
            ent = report.f.get(oid)
            picture = svg(lines, width, height, ent.type if ent is not None else "",
                          limit=REPORT_SEGMENTS)
            if picture:
                out[oid] = picture
    return out
