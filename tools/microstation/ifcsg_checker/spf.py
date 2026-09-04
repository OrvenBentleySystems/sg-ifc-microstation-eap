"""ISO-10303-21 (STEP Physical File) reader for IFC, with no third-party dependencies.

Deterministic by construction: no inference, no tolerance-based matching, no guessing.
Semantic entities are stored verbatim; attribute access is lazy and cached.
Large tessellation records can be counted without being retained.
"""

import io
import os
import sys
import zipfile


NULL_TOKENS = ("$", "*")
READ_CHUNK = 1024 * 1024
MAX_HEADER_CHARS = 8 * 1024 * 1024

# These records dominate exported architectural models but are not needed by the
# IFC+SG semantic rules. Their counts are retained for QC.001. Full geometry mode
# remains available for specialist diagnostics outside the MicroStation UI.
HEAVY_GEOMETRY_TYPES = {
    "IFCCARTESIANPOINT",
    "IFCCARTESIANPOINTLIST2D",
    "IFCCARTESIANPOINTLIST3D",
    "IFCCLOSEDSHELL",
    "IFCCONNECTEDFACESET",
    "IFCFACE",
    "IFCFACEBOUND",
    "IFCFACEOUTERBOUND",
    "IFCFACETEDBREP",
    "IFCFACETEDBREPWITHVOIDS",
    "IFCINDEXEDPOLYGONALFACE",
    "IFCINDEXEDPOLYGONALFACEWITHVOIDS",
    "IFCPOLYGONALFACESET",
    "IFCPOLYLOOP",
    "IFCTRIANGULATEDFACESET",
}


class OperationCancelled(Exception):
    """Raised when a caller cancels a long-running parse or rule evaluation."""


class UnsupportedIfcFormat(ValueError):
    """Raised for a source container this SPF reader cannot interpret."""


class EntityStore(object):
    """Paged integer-key store with much lower overhead than a 3M-entry dict."""

    __slots__ = ("_pages", "_count")
    PAGE_BITS = 12
    PAGE_SIZE = 1 << PAGE_BITS
    PAGE_MASK = PAGE_SIZE - 1

    def __init__(self):
        self._pages = {}
        self._count = 0

    def __setitem__(self, eid, entity):
        page_id = eid >> self.PAGE_BITS
        page = self._pages.get(page_id)
        if page is None:
            page = [None] * self.PAGE_SIZE
            self._pages[page_id] = page
        slot = eid & self.PAGE_MASK
        if page[slot] is None:
            self._count += 1
        page[slot] = entity

    def get(self, eid, default=None):
        if eid is None or eid < 0:
            return default
        page = self._pages.get(eid >> self.PAGE_BITS)
        if page is None:
            return default
        value = page[eid & self.PAGE_MASK]
        return default if value is None else value

    def values(self):
        for page_id in sorted(self._pages):
            for entity in self._pages[page_id]:
                if entity is not None:
                    yield entity

    def __len__(self):
        return self._count


def source_text_size(path):
    """Uncompressed SPF byte size used for progress and memory preflight."""
    ext = os.path.splitext(path)[1].lower()
    if ext != ".ifczip":
        return os.path.getsize(path)
    with zipfile.ZipFile(path, "r") as archive:
        members = [info for info in archive.infolist()
                   if not info.is_dir() and info.filename.lower().endswith(".ifc")]
        if not members:
            raise UnsupportedIfcFormat("IFCZIP contains no .ifc file.")
        return max(info.file_size for info in members)

# IFC4 attribute index of PredefinedType, per entity. None means "not defined for
# this entity"; absence from the table means "layout unknown -> report UNKNOWN".
PREDEFINED_TYPE_INDEX = {
    "IFCBEAM": 8, "IFCBEAMSTANDARDCASE": 8,
    "IFCBUILDINGELEMENTPROXY": 8,
    "IFCCHIMNEY": 8, "IFCCOLUMN": 8, "IFCCOLUMNSTANDARDCASE": 8,
    "IFCCOVERING": 8, "IFCCURTAINWALL": 8, "IFCDISCRETEACCESSORY": 8,
    "IFCFOOTING": 8, "IFCFURNITURE": 8, "IFCGEOGRAPHICELEMENT": 8,
    "IFCMEMBER": 8, "IFCMEMBERSTANDARDCASE": 8,
    "IFCPILE": 8, "IFCPLATE": 8, "IFCPLATESTANDARDCASE": 8,
    "IFCRAILING": 8, "IFCRAMP": 8, "IFCRAMPFLIGHT": 8, "IFCROOF": 8,
    "IFCSHADINGDEVICE": 8, "IFCSLAB": 8, "IFCSLABSTANDARDCASE": 8,
    "IFCSTAIR": 8, "IFCTRANSPORTELEMENT": 8,
    "IFCWALL": 8, "IFCWALLSTANDARDCASE": 8, "IFCOPENINGELEMENT": 8,
    "IFCSENSOR": 8, "IFCALARM": 8, "IFCAIRTERMINAL": 8, "IFCDAMPER": 8,
    "IFCDUCTSEGMENT": 8, "IFCDUCTFITTING": 8, "IFCPIPESEGMENT": 8,
    "IFCPIPEFITTING": 8, "IFCSANITARYTERMINAL": 8, "IFCFLOWMETER": 8,
    "IFCVALVE": 8, "IFCPUMP": 8, "IFCTANK": 8, "IFCFAN": 8, "IFCFILTER": 8,
    "IFCCABLECARRIERSEGMENT": 8, "IFCCABLESEGMENT": 8, "IFCLIGHTFIXTURE": 8,
    "IFCSWITCHINGDEVICE": 8, "IFCFIRESUPPRESSIONTERMINAL": 8,
    "IFCWASTETERMINAL": 8, "IFCDISTRIBUTIONCHAMBERELEMENT": 8,
    "IFCDOOR": 10, "IFCDOORSTANDARDCASE": 10,
    "IFCWINDOW": 10, "IFCWINDOWSTANDARDCASE": 10,
    "IFCELEMENTASSEMBLY": 9,
    "IFCSTAIRFLIGHT": 12,
    "IFCSPACE": 9,
    "IFCCIVILELEMENT": None,
    "IFCELEMENTASSEMBLYTYPE": None,
}

# Secondary enumerated attribute that IFC+SG relies on for the same entity.
SECONDARY_TYPE_INDEX = {
    "IFCDOOR": ("OperationType", 11),
    "IFCWINDOW": ("PartitioningType", 11),
    "IFCPILE": ("ConstructionType", 9),
    "IFCELEMENTASSEMBLY": ("AssemblyPlace", 8),
}

# Attribute indices shared by every IfcRoot / IfcObject subtype.
IDX_GLOBALID = 0
IDX_NAME = 2
IDX_DESCRIPTION = 3
IDX_OBJECTTYPE = 4
IDX_PLACEMENT = 5
IDX_REPRESENTATION = 6
IDX_TAG = 7


def split_args(body):
    """Split a STEP attribute list at top level, honouring quotes and nesting."""
    out = []
    buf = []
    depth = 0
    in_str = False
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if in_str:
            buf.append(c)
            if c == "'":
                if i + 1 < n and body[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
        elif c == "'":
            in_str = True
            buf.append(c)
        elif c == "(":
            depth += 1
            buf.append(c)
        elif c == ")":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf).strip())
    return out


def unquote(tok):
    """Return the plain value of a STEP token, or None for $ and *."""
    if tok is None:
        return None
    tok = tok.strip()
    if tok == "" or tok in NULL_TOKENS:
        return None
    if len(tok) >= 2 and tok[0] == "'" and tok[-1] == "'":
        return _decode(tok[1:-1].replace("''", "'"))
    if len(tok) >= 2 and tok[0] == "." and tok[-1] == ".":
        return tok[1:-1]
    return tok


def _decode(s):
    """Decode the ISO-10303-21 \\X2\\ and \\S\\ extended string encodings."""
    if "\\X" not in s and "\\S\\" not in s:
        return s
    out = []
    i = 0
    n = len(s)
    while i < n:
        if s.startswith("\\X2\\", i):
            j = s.find("\\X0\\", i)
            if j < 0:
                out.append(s[i:])
                break
            hexs = s[i + 4:j]
            for k in range(0, len(hexs) - 3, 4):
                try:
                    out.append(chr(int(hexs[k:k + 4], 16)))
                except ValueError:
                    pass
            i = j + 4
        elif s.startswith("\\X\\", i) and i + 5 <= n:
            try:
                out.append(chr(int(s[i + 3:i + 5], 16)))
            except ValueError:
                pass
            i += 5
        elif s.startswith("\\S\\", i) and i + 4 <= n:
            out.append(chr(ord(s[i + 3]) + 128))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def typed_value(tok):
    """Split TYPE(value) into ('TYPE', value). Returns (None, None) for $."""
    if tok is None:
        return (None, None)
    tok = tok.strip()
    if tok in NULL_TOKENS or tok == "":
        return (None, None)
    op = tok.find("(")
    if op > 0 and tok.endswith(")"):
        return (tok[:op].strip().upper(), unquote(tok[op + 1:-1]))
    return (None, unquote(tok))


def as_ref(tok):
    """Return the integer id of a '#123' token, else None."""
    if tok is None:
        return None
    tok = tok.strip()
    if not tok.startswith("#"):
        return None
    try:
        return int(tok[1:])
    except ValueError:
        return None


def as_refs(tok):
    """Return every '#123' id in an aggregate token, in file order."""
    if tok is None:
        return []
    tok = tok.strip()
    if tok.startswith("(") and tok.endswith(")"):
        tok = tok[1:-1]
    out = []
    for part in split_args(tok):
        rid = as_ref(part)
        if rid is not None:
            out.append(rid)
    return out


def as_float(tok):
    v = unquote(tok)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Entity(object):
    __slots__ = ("id", "type", "body", "_args")

    def __init__(self, eid, etype, body):
        self.id = eid
        self.type = etype
        self.body = body
        self._args = None

    @property
    def args(self):
        if self._args is None:
            self._args = split_args(self.body)
        return self._args

    def arg(self, index):
        a = self.args
        return a[index] if 0 <= index < len(a) else None

    def value(self, index):
        return unquote(self.arg(index))

    def __repr__(self):
        return "#%d=%s" % (self.id, self.type)


class IfcFile(object):
    """A parsed IFC SPF file."""

    def __init__(self, path, retain_geometry=False):
        self.path = path
        self.retain_geometry = bool(retain_geometry)
        self.geometry_available = bool(retain_geometry)
        self.entities = EntityStore()
        self.header_text = ""
        self.schema = None
        self.descriptions = []
        self.view_definitions = []
        self.originating_system = None
        self.preprocessor = None
        self.timestamp = None
        self.byte_size = 0
        self.parse_error = None
        self._by_type = None
        self._type_counts = {}
        self._total_count = 0

    @classmethod
    def read(cls, path, progress=None, cancel=None, retain_geometry=False):
        inst = cls(path, retain_geometry=retain_geometry)
        if not path or not os.path.isfile(path):
            raise OSError("IFC source does not exist: %s" % (path or "(empty path)"))

        ext = os.path.splitext(path)[1].lower()
        if ext == ".ifcxml":
            raise UnsupportedIfcFormat(
                "IFCXML is not supported. Export or save the model as IFC-SPF (.ifc).")

        if ext == ".ifczip":
            with zipfile.ZipFile(path, "r") as archive:
                members = [info for info in archive.infolist()
                           if not info.is_dir() and info.filename.lower().endswith(".ifc")]
                if not members:
                    raise UnsupportedIfcFormat("IFCZIP contains no .ifc file.")
                members.sort(key=lambda info: (-info.file_size, info.filename.lower()))
                member = members[0]
                inst.byte_size = member.file_size
                with archive.open(member, "r") as raw:
                    with io.TextIOWrapper(raw, encoding="utf-8", errors="replace") as fh:
                        inst._parse_stream(fh, progress=progress, cancel=cancel,
                                           total=member.file_size)
        else:
            if ext != ".ifc":
                raise UnsupportedIfcFormat(
                    "Unsupported IFC source '%s'. Choose an .ifc or .ifczip file." % ext)
            inst.byte_size = source_text_size(path)
            with open(path, "r", encoding="utf-8", errors="replace",
                      newline="") as fh:
                inst._parse_stream(fh, progress=progress, cancel=cancel,
                                   total=inst.byte_size)
        return inst

    def _parse(self, text):
        """Parse in-memory text. Kept for focused tests; file reads stream."""
        self.byte_size = len(text.encode("utf-8", errors="replace"))
        self._parse_stream(io.StringIO(text), total=len(text))

    def _parse_stream(self, fh, progress=None, cancel=None, total=None):
        """Parse SPF incrementally so large real models do not duplicate the file in RAM."""
        entities = self.entities
        buf = []
        in_str = False
        in_comment = False
        quote_pending = False
        slash_pending = False
        comment_star = False
        data_started = False
        header = []
        processed = 0
        next_progress = 0

        while True:
            if cancel is not None and cancel():
                raise OperationCancelled("Operation cancelled.")
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            processed += len(chunk)

            if not data_started:
                header.append(chunk)
                joined = "".join(header)
                cut = joined.find("DATA;")
                if cut < 0:
                    if len(joined) > MAX_HEADER_CHARS:
                        raise ValueError("No DATA section found in the first 8 MB.")
                    self._notify_progress(progress, processed, total, next_progress)
                    next_progress = processed + READ_CHUNK
                    continue
                self.header_text = joined[:cut]
                self._read_header()
                chunk = joined[cut + 5:]
                header = None
                data_started = True

            for c in chunk:
                if in_comment:
                    if comment_star:
                        if c == "/":
                            in_comment = False
                            comment_star = False
                            continue
                        comment_star = c == "*"
                    elif c == "*":
                        comment_star = True
                    continue

                if in_str:
                    if quote_pending:
                        if c == "'":
                            buf.extend(("'", "'"))
                            quote_pending = False
                            continue
                        buf.append("'")
                        quote_pending = False
                        in_str = False
                    else:
                        if c == "'":
                            quote_pending = True
                        else:
                            buf.append(c)
                        continue

                if slash_pending:
                    if c == "*":
                        slash_pending = False
                        in_comment = True
                        continue
                    buf.append("/")
                    slash_pending = False

                if c == "'":
                    in_str = True
                    buf.append(c)
                elif c == "/":
                    slash_pending = True
                elif c == ";":
                    self._emit("".join(buf), entities)
                    buf = []
                elif c not in "\r\n\t":
                    buf.append(c)

            if progress is not None and processed >= next_progress:
                self._notify_progress(progress, processed, total, next_progress)
                next_progress = processed + READ_CHUNK

        if not data_started:
            self.header_text = "".join(header)[:4096]
            self._read_header()
            raise ValueError("Not an IFC-SPF file: DATA section is missing.")
        if quote_pending:
            buf.append("'")
        if slash_pending:
            buf.append("/")
        self._notify_progress(progress, total or processed, total, next_progress, force=True)

    @staticmethod
    def _notify_progress(callback, completed, total, _next, force=False):
        if callback is None:
            return
        if force or completed >= _next:
            callback(min(completed, total) if total else completed, total)

    def _emit(self, stmt, entities):
        stmt = stmt.strip()
        if len(stmt) < 4 or stmt[0] != "#":
            return
        eq = stmt.find("=")
        if eq < 1:
            return
        try:
            eid = int(stmt[1:eq])
        except ValueError:
            return
        rest = stmt[eq + 1:].strip()
        op = rest.find("(")
        if op < 1 or not rest.endswith(")"):
            return
        etype = sys.intern(rest[:op].strip().upper())
        self._type_counts[etype] = self._type_counts.get(etype, 0) + 1
        self._total_count += 1
        if not self.retain_geometry and etype in HEAVY_GEOMETRY_TYPES:
            return
        entities[eid] = Entity(eid, etype, rest[op + 1:-1])

    def _read_header(self):
        h = " ".join(_strip_comments(self.header_text).split())

        schema = _group_after(h, "FILE_SCHEMA")
        if schema:
            names = [unquote(x) for x in split_args(_unwrap(schema))]
            self.schema = " ".join(n for n in names if n)

        desc = _group_after(h, "FILE_DESCRIPTION")
        if desc:
            parts = split_args(desc)
            if parts:
                for item in split_args(_unwrap(parts[0])):
                    v = unquote(item)
                    if not v:
                        continue
                    self.descriptions.append(v)
                    if v.upper().replace(" ", "").startswith("VIEWDEFINITION"):
                        self.view_definitions.append(v)

        name = _group_after(h, "FILE_NAME")
        if name:
            parts = split_args(name)
            if len(parts) > 1:
                self.timestamp = unquote(parts[1])
            if len(parts) > 4:
                self.preprocessor = unquote(parts[4])
            if len(parts) > 5:
                self.originating_system = unquote(parts[5])

    # -- lookup -------------------------------------------------------------

    def _index(self):
        if self._by_type is None:
            idx = {}
            for e in self.entities.values():
                idx.setdefault(e.type, []).append(e)
            for lst in idx.values():
                lst.sort(key=lambda x: x.id)
            self._by_type = idx
        return self._by_type

    def of_type(self, name):
        return self._index().get(name.upper(), [])

    def of_types(self, names):
        out = []
        for n in names:
            out.extend(self.of_type(n))
        out.sort(key=lambda x: x.id)
        return out

    def get(self, eid):
        return self.entities.get(eid)

    def type_of(self, eid):
        e = self.entities.get(eid)
        return e.type if e is not None else None

    def type_counts(self):
        return dict(self._type_counts)

    def count(self):
        return self._total_count

    def retained_count(self):
        return len(self.entities)


def _strip_comments(text):
    out = []
    i = 0
    n = len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 1
                else:
                    in_str = False
        elif c == "'":
            in_str = True
            out.append(c)
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 1
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _group_after(text, keyword):
    """Return the contents of the balanced ( ... ) that follows a header keyword."""
    i = text.find(keyword)
    if i < 0:
        return None
    j = text.find("(", i + len(keyword))
    if j < 0:
        return None
    depth = 0
    in_str = False
    k = j
    n = len(text)
    while k < n:
        c = text[k]
        if in_str:
            if c == "'":
                if k + 1 < n and text[k + 1] == "'":
                    k += 1
                else:
                    in_str = False
        elif c == "'":
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[j + 1:k]
        k += 1
    return None


def _unwrap(token):
    token = (token or "").strip()
    if token.startswith("(") and token.endswith(")"):
        return token[1:-1]
    return token


# ---------------------------------------------------------------------------
# Geometry: placement chains and bounding boxes, used only to locate elements.
# ---------------------------------------------------------------------------

IDENTITY = (1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0)

_POINT_SCAN_CAP = 400


def mat_multiply(a, b):
    """Compose two 3x4 row-major affine matrices: result = a applied after b."""
    out = []
    for r in range(3):
        ar = a[r * 4:r * 4 + 4]
        for c in range(3):
            out.append(ar[0] * b[c] + ar[1] * b[4 + c] + ar[2] * b[8 + c])
        out.append(ar[0] * b[3] + ar[1] * b[7] + ar[2] * b[11] + ar[3])
    return tuple(out)


def mat_apply(m, p):
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def _normalise(v):
    mag = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) ** 0.5
    if mag == 0.0:
        return None
    return (v[0] / mag, v[1] / mag, v[2] / mag)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


class Geometry(object):
    """Resolves IfcProduct placements and bounding boxes into model coordinates.

    Coordinates are returned in the file's own length unit, unconverted.
    """

    def __init__(self, ifc):
        self.f = ifc
        self._placement_cache = {}
        self._bbox_cache = {}

    # -- placements ---------------------------------------------------------

    def point_coords(self, pid):
        e = self.f.get(pid) if pid is not None else None
        if e is None or e.type != "IFCCARTESIANPOINT":
            return None
        vals = as_refs_free(e.arg(0))
        return (vals + [0.0, 0.0, 0.0])[:3] if vals else None

    def direction(self, did, default):
        e = self.f.get(did) if did is not None else None
        if e is None or e.type != "IFCDIRECTION":
            return default
        vals = as_refs_free(e.arg(0))
        if not vals:
            return default
        vals = (vals + [0.0, 0.0, 0.0])[:3]
        return _normalise(tuple(vals)) or default

    def axis_matrix(self, aid):
        """Build a 3x4 matrix from IfcAxis2Placement3D / 2D."""
        e = self.f.get(aid) if aid is not None else None
        if e is None:
            return IDENTITY
        loc = self.point_coords(as_ref(e.arg(0))) or [0.0, 0.0, 0.0]
        if e.type == "IFCAXIS2PLACEMENT2D":
            xd = self.direction(as_ref(e.arg(1)), (1.0, 0.0, 0.0))
            return (xd[0], -xd[1], 0.0, loc[0],
                    xd[1], xd[0], 0.0, loc[1],
                    0.0, 0.0, 1.0, loc[2] if len(loc) > 2 else 0.0)
        zd = self.direction(as_ref(e.arg(1)), (0.0, 0.0, 1.0))
        xr = self.direction(as_ref(e.arg(2)), (1.0, 0.0, 0.0))
        proj = _dot(xr, zd)
        xd = _normalise((xr[0] - proj * zd[0], xr[1] - proj * zd[1], xr[2] - proj * zd[2]))
        if xd is None:
            xd = (1.0, 0.0, 0.0) if abs(zd[2]) < 0.9 else (1.0, 0.0, 0.0)
        yd = _cross(zd, xd)
        return (xd[0], yd[0], zd[0], loc[0],
                xd[1], yd[1], zd[1], loc[1],
                xd[2], yd[2], zd[2], loc[2] if len(loc) > 2 else 0.0)

    def placement_matrix(self, pid, _depth=0):
        """Resolve an IfcLocalPlacement chain to a world matrix."""
        if pid is None or _depth > 64:
            return IDENTITY
        cached = self._placement_cache.get(pid)
        if cached is not None:
            return cached
        e = self.f.get(pid)
        if e is None:
            return IDENTITY
        if e.type == "IFCLOCALPLACEMENT":
            parent = self.placement_matrix(as_ref(e.arg(0)), _depth + 1)
            local = self.axis_matrix(as_ref(e.arg(1)))
            m = mat_multiply(parent, local)
        elif e.type in ("IFCAXIS2PLACEMENT3D", "IFCAXIS2PLACEMENT2D"):
            m = self.axis_matrix(pid)
        else:
            m = IDENTITY
        self._placement_cache[pid] = m
        return m

    # -- bounding boxes -----------------------------------------------------

    def local_points(self, product):
        """Collect representation points in the product's local frame."""
        rep_id = as_ref(product.arg(IDX_REPRESENTATION))
        rep = self.f.get(rep_id) if rep_id else None
        if rep is None or rep.type != "IFCPRODUCTDEFINITIONSHAPE":
            return []
        pts = []
        box_pts = []
        for sr_id in as_refs(rep.arg(2)):
            sr = self.f.get(sr_id)
            if sr is None or sr.type != "IFCSHAPEREPRESENTATION":
                continue
            ident = (unquote(sr.arg(1)) or "").upper()
            items = as_refs(sr.arg(3))
            if ident == "BOX":
                for it in items:
                    bb = self.f.get(it)
                    if bb is None or bb.type != "IFCBOUNDINGBOX":
                        continue
                    corner = self.point_coords(as_ref(bb.arg(0)))
                    xd = as_float(bb.arg(1))
                    yd = as_float(bb.arg(2))
                    zd = as_float(bb.arg(3))
                    if corner and None not in (xd, yd, zd):
                        box_pts.append(tuple(corner))
                        box_pts.append((corner[0] + xd, corner[1] + yd, corner[2] + zd))
            elif not box_pts:
                self._scan_points(items, pts, [0])
        return box_pts if box_pts else pts

    def _scan_points(self, ids, sink, budget, depth=0):
        if depth > 12:
            return
        for eid in ids:
            if budget[0] >= _POINT_SCAN_CAP:
                return
            e = self.f.get(eid)
            if e is None:
                continue
            t = e.type
            if t == "IFCCARTESIANPOINT":
                c = self.point_coords(eid)
                if c:
                    sink.append(tuple((c + [0.0, 0.0, 0.0])[:3]))
                    budget[0] += 1
            elif t in ("IFCCARTESIANPOINTLIST3D", "IFCCARTESIANPOINTLIST2D"):
                for tup in _nested_floats(e.arg(0)):
                    sink.append(tuple((tup + [0.0, 0.0, 0.0])[:3]))
                    budget[0] += 1
                    if budget[0] >= _POINT_SCAN_CAP:
                        return
            else:
                nested = []
                for a in e.args:
                    nested.extend(as_refs(a) if a.strip().startswith("(") else _single(a))
                if nested:
                    self._scan_points(nested, sink, budget, depth + 1)

    def world_box(self, product):
        """Return ((lx,ly,lz),(hx,hy,hz)) in model coordinates, or None."""
        key = product.id
        if key in self._bbox_cache:
            return self._bbox_cache[key]
        m = self.placement_matrix(as_ref(product.arg(IDX_PLACEMENT)))
        pts = self.local_points(product)
        if pts:
            world = [mat_apply(m, p) for p in pts]
            lo = (min(p[0] for p in world), min(p[1] for p in world), min(p[2] for p in world))
            hi = (max(p[0] for p in world), max(p[1] for p in world), max(p[2] for p in world))
            box = (lo, hi)
        else:
            o = (m[3], m[7], m[11])
            box = (o, o)
        self._bbox_cache[key] = box
        return box

    def world_point(self, product):
        lo, hi = self.world_box(product)
        return ((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, (lo[2] + hi[2]) * 0.5)


def _single(tok):
    r = as_ref(tok)
    return [r] if r is not None else []


def as_refs_free(tok):
    """Parse a bare numeric aggregate such as (1.0,2.0,3.0)."""
    if tok is None:
        return []
    tok = tok.strip()
    if tok.startswith("(") and tok.endswith(")"):
        tok = tok[1:-1]
    out = []
    for part in split_args(tok):
        v = as_float(part)
        if v is not None:
            out.append(v)
    return out


def _nested_floats(tok):
    """Parse ((x,y,z),(x,y,z),...) into a list of float lists."""
    if tok is None:
        return []
    tok = tok.strip()
    if tok.startswith("(") and tok.endswith(")"):
        tok = tok[1:-1]
    out = []
    for part in split_args(tok):
        vals = as_refs_free(part)
        if vals:
            out.append(vals)
    return out
