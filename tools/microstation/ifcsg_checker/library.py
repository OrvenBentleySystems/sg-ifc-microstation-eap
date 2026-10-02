"""Load and resolve IFC+SG checker catalogues, one per CORENET X COP edition."""

import datetime
import difflib
import glob
import json
import os
import re

EXACT = "EXACT"
CASE_MISMATCH = "CASE_MISMATCH"
NEAR_MISS = "NEAR_MISS"
UNKNOWN = "UNKNOWN"

CATALOGUE_DIR = "catalogues"
CATALOGUE_NAME = "catalogue.json"          # legacy single-file layout
CATALOGUE_FORMAT = "ifcsg-checker-catalogue"
CATALOGUE_FORMAT_VERSION = 1
PROPERTY_TYPES = {
    "Label", "Text", "Boolean", "Integer", "Real", "Length", "Area",
    "Volume", "Count", "Identifier", "Mass", "Time", "Unknown",
}

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_PKG_DIR)))
DEFAULT_ROOTS = (_TOOL_ROOT, os.environ.get("IFCSG_CHECKER_DIR", ""))


def _norm(name):
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _token(value):
    return re.sub(r"\s+", "", str(value or "")).upper()


def base_entity(entity):
    """IfcWallStandardCase -> IFCWALL: the mapping names supertypes only."""
    name = str(entity or "").upper()
    for suffix in ("STANDARDCASE", "ELEMENTEDCASE"):
        if name.endswith(suffix) and len(name) > len(suffix) + 3:
            return name[:-len(suffix)]
    return name


def cop_key(edition):
    """Sortable key for a COP edition string such as '3.1' or '4'."""
    numbers = [int(part) for part in re.findall(r"\d+", str(edition or ""))]
    return tuple(numbers) or (0,)


def same_cop(a, b):
    return cop_key(a) == cop_key(b) or str(a).strip().lower() == str(b).strip().lower()


def catalogue_paths(root):
    """Every catalogue file under root, in no particular order."""
    if not root:
        return []
    paths = sorted(glob.glob(os.path.join(root, "data", CATALOGUE_DIR, "*.json")))
    legacy = os.path.join(root, "data", CATALOGUE_NAME)
    if not paths and os.path.isfile(legacy):
        paths = [legacy]
    return paths


def _read_metadata(path):
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if payload.get("format") != CATALOGUE_FORMAT:
        return None
    return payload.get("metadata") or {}


class Edition(object):
    """Summary of one installed catalogue, used by the COP selector."""

    __slots__ = ("cop_edition", "mapping_edition", "cop_published", "title", "path")

    def __init__(self, path, metadata):
        self.path = path
        self.cop_edition = str(metadata.get("cop_edition") or "unknown")
        self.mapping_edition = str(metadata.get("mapping_edition") or "unknown")
        self.cop_published = str(metadata.get("cop_published") or "")
        self.title = str(metadata.get("cop_title") or "")

    @property
    def label(self):
        published = " (%s)" % self.cop_published if self.cop_published else ""
        return "COP %s%s  |  mapping %s" % (
            self.cop_edition, published, self.mapping_edition)

    def __repr__(self):
        return "<Edition COP %s mapping %s>" % (self.cop_edition, self.mapping_edition)


class Resolution(object):
    __slots__ = ("status", "value", "candidates", "note")

    def __init__(self, status, value=None, candidates=None, note=""):
        self.status = status
        self.value = value
        self.candidates = candidates or []
        self.note = note

    @property
    def ok(self):
        return self.status == EXACT

    def __repr__(self):
        return "<%s %s>" % (self.status, self.value)


class Library(object):
    """Validated runtime view of one ``data/catalogues/cop-*.json`` file."""

    def __init__(self, root, path=None):
        self.root = root
        if path is None:
            found = Library.available(root)
            if not found:
                raise IOError("No IFC+SG catalogue under %s" % root)
            path = found[0].path
        self.path = path
        self.load_errors = []
        self.catalogue = self._read()
        self.metadata = self.catalogue.get("metadata", {})
        self.latest_cop = self.metadata.get("cop_edition")
        self.sgpsets = {}
        self.sgpsets_by_norm = {}
        self.property_owners = {}
        self.identified_components = self.catalogue.get("identified_components", [])
        self._domain_index = None
        self._index()

    @staticmethod
    def find_root(explicit_root=None):
        roots = ([explicit_root] if explicit_root else []) + list(DEFAULT_ROOTS)
        for root in roots:
            if root and catalogue_paths(root):
                return root
        raise IOError(
            "IFC+SG catalogue not found. Looked for data/%s/*.json under: %s"
            % (CATALOGUE_DIR, ", ".join(x for x in roots if x)))

    @staticmethod
    def available(explicit_root=None):
        """Installed COP editions, newest first."""
        root = explicit_root if explicit_root and catalogue_paths(explicit_root) \
            else Library.find_root(explicit_root)
        out = []
        for path in catalogue_paths(root):
            try:
                metadata = _read_metadata(path)
            except (OSError, ValueError):
                continue
            if metadata is not None:
                out.append(Edition(path, metadata))
        out.sort(key=lambda item: (cop_key(item.cop_edition), item.mapping_edition),
                 reverse=True)
        return out

    @staticmethod
    def discover(explicit_root=None, cop=None):
        """Load the catalogue for a COP edition; the newest when cop is None.

        An unknown edition raises rather than silently checking against a
        different code of practice.
        """
        root = Library.find_root(explicit_root)
        editions = Library.available(root)
        if not editions:
            raise IOError("No readable IFC+SG catalogue under %s" % root)
        chosen = editions[0]
        if cop:
            matches = [item for item in editions if same_cop(item.cop_edition, cop)]
            if not matches:
                raise ValueError(
                    "COP edition '%s' is not installed. Available: %s"
                    % (cop, ", ".join(item.cop_edition for item in editions)))
            chosen = matches[0]
        library = Library(root, chosen.path)
        library.latest_cop = editions[0].cop_edition
        return library

    @property
    def is_superseded(self):
        return bool(self.latest_cop) and not same_cop(
            self.latest_cop, self.metadata.get("cop_edition"))

    def _read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            raise IOError("Cannot read %s: %s" % (self.path, exc))
        if payload.get("format") != CATALOGUE_FORMAT:
            raise ValueError("Unsupported catalogue format: %s"
                             % payload.get("format", "(missing)"))
        if payload.get("format_version") != CATALOGUE_FORMAT_VERSION:
            raise ValueError("Unsupported catalogue format version: %s"
                             % payload.get("format_version", "(missing)"))
        for key in ("metadata", "property_sets", "area_schemes",
                    "entity_domains", "identified_components", "rules"):
            if key not in payload:
                raise ValueError("Catalogue is missing '%s'." % key)
        self._validate(payload)
        return payload

    @staticmethod
    def _validate(payload):
        seen_sets = set()
        for pset in payload["property_sets"]:
            name = pset.get("name")
            if not name or not name.startswith(("SGPset_", "Pset_", "Qto_")):
                raise ValueError("Invalid property set name: %r" % name)
            if name in seen_sets:
                raise ValueError("Duplicate property set: %s" % name)
            seen_sets.add(name)
            seen_props = set()
            for prop in pset.get("properties", []):
                prop_name = prop.get("name")
                if not prop_name:
                    raise ValueError("%s contains an unnamed property." % name)
                if prop_name in seen_props:
                    raise ValueError("Duplicate property: %s.%s" % (name, prop_name))
                seen_props.add(prop_name)
                if prop.get("type") not in PROPERTY_TYPES:
                    raise ValueError("Unsupported datatype for %s.%s: %s"
                                     % (name, prop_name, prop.get("type")))
        rule_ids = [rule.get("id") for rule in payload["rules"]]
        if any(not rule_id for rule_id in rule_ids):
            raise ValueError("Every rule must have an id.")
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("Duplicate validation rule id.")

    def _index(self):
        entries = list(self.catalogue.get("property_sets", []))
        for scheme in self.catalogue.get("area_schemes", []):
            name = scheme.get("property_set")
            if not name or any(item.get("name") == name for item in entries):
                continue
            entries.append({
                "name": name,
                "binding": "I",
                "entities": ["IfcSpace"],
                "subtypes": [scheme.get("subtype")] if scheme.get("subtype") else [],
                "verified": True,
                "source": "catalogue.area_schemes",
                "properties": scheme.get("properties", []),
                "object_type_token": scheme.get("object_type_token"),
            })
        for entry in entries:
            name = entry.get("name")
            if not name:
                continue
            self.sgpsets[name] = entry
            self.sgpsets_by_norm.setdefault(_norm(name), name)
            for prop in entry.get("properties", []):
                pname = prop.get("name")
                if pname:
                    self.property_owners.setdefault(pname, set()).add(name)

    @property
    def is_complete(self):
        return bool(self.metadata.get("complete"))

    @property
    def provenance(self):
        return self.metadata

    @property
    def editions(self):
        mapping = self.metadata.get("mapping_edition") or "NOT SET"
        cop = self.metadata.get("cop_edition") or "NOT SET"
        sources = self.metadata.get("sources", [])
        return {
            "mapping_edition": mapping,
            "cop_edition": cop,
            "library_version": self.catalogue.get("version", "1.0.0"),
            "sgpset_source": "catalogue",
            "sgpset_count": len(self.sgpsets),
            "untranscribed_count": 0,
            "built_at": self.metadata.get("built_at", ""),
            "overrides": "-",
            "identified_components": len(self.identified_components),
            "source_files": ", ".join(
                "%s (%s)" % (source.get("name", "?"), source.get("kind", "?"))
                for source in sources) or "-",
            "mapping_age_days": self.mapping_age_days(),
            "authoritative_mapping": bool(self.metadata.get("authoritative_mapping")),
            "cop_published": self.metadata.get("cop_published") or "",
            "latest_cop": self.latest_cop or cop,
            "superseded": self.is_superseded,
        }

    def mapping_age_days(self):
        raw = self.metadata.get("mapping_edition")
        if not raw:
            return None
        try:
            edition = datetime.datetime.strptime(str(raw)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
        return (datetime.date.today() - edition).days

    def rules(self):
        return self.catalogue.get("rules", [])

    def rule(self, rule_id):
        for rule in self.rules():
            if rule.get("id") == rule_id:
                return rule
        return {}

    def area_schemes(self):
        return self.catalogue.get("area_schemes", [])

    def identified_components_for(self, ifc_entity):
        want = (ifc_entity or "").upper()
        return [
            item.get("name")
            for item in self.identified_components
            if str(item.get("entity") or "").upper() == want and item.get("name")
        ]

    def component_variants(self, ifc_entity):
        """[(component, subtype tokens, property sets)] the mapping gives an entity."""
        want = base_entity(ifc_entity)
        out = []
        for item in self.identified_components:
            if base_entity(item.get("entity")) != want:
                continue
            variants = item.get("variants") or [{
                "subtypes": item.get("subtypes") or [],
                "property_sets": item.get("property_sets") or []}]
            for variant in variants:
                psets = [p for p in variant.get("property_sets") or [] if p in self.sgpsets]
                if psets:
                    out.append((item.get("name") or "?",
                                [_token(t) for t in variant.get("subtypes") or []],
                                psets))
        return out

    def valid_subtypes(self, ifc_entity):
        """Every subtype token (without '*') that the COP gives an entity.

        Includes tokens of components that carry no property set and tokens the
        COP document lists where the workbook does not.
        """
        want = base_entity(ifc_entity)
        out = set()
        for item in self.identified_components:
            if base_entity(item.get("entity")) == want:
                for token in item.get("subtypes") or []:
                    out.add(_token(token).lstrip("*"))
        for entity, tokens in (self.catalogue.get("cop_pdf_subtypes") or {}).items():
            if base_entity(entity) == want:
                out.update(_token(t).lstrip("*") for t in tokens)
        out.discard("")
        return out

    def required_psets(self, ifc_entity, token=None):
        """Property sets an element must carry, and why.

        Returns None when the mapping has no identified component for the entity.
        Otherwise (sets, basis). A declared subtype selects the matching
        identified component. Without one, only the sets that every candidate
        component needs are required, so an undeclared subtype never inflates
        the requirement to the union of unrelated components.
        """
        variants = self.component_variants(ifc_entity)
        if not variants:
            return None
        key = _token(token) if token else ""

        def hit(subtypes):
            return bool(key) and (key in subtypes or key.lstrip("*") in
                                  [t.lstrip("*") for t in subtypes])

        by_component = {}
        for name, subtypes, psets in variants:
            entry = by_component.setdefault(name, {"generic": set(), "matched": set(),
                                                   "all": set(), "hit": False})
            entry["all"].update(psets)
            if not subtypes:
                entry["generic"].update(psets)
            elif hit(subtypes):
                entry["matched"].update(psets)
                entry["hit"] = True

        # Within one component the workbook spreads its sets over several rows:
        # the general (N.A) rows apply to every subtype, the subtype rows add to
        # them. Across different components only the common sets are certain.
        matched = [n for n, e in by_component.items() if e["hit"]]
        if matched:
            pools = [by_component[n]["generic"] | by_component[n]["matched"] for n in matched]
            basis = "identified component %s (subtype %s)" % (" / ".join(sorted(matched)), token)
        else:
            generic = [n for n, e in by_component.items() if e["generic"]]
            names = sorted(generic or by_component)
            pools = [by_component[n]["generic"] if generic else by_component[n]["all"]
                     for n in names]
            label = (", ".join(names) if len(names) <= 4
                     else "all %d candidate components" % len(names))
            if generic:
                basis = "the general %s component (any subtype)" % label
            else:
                reason = ("subtype %s is not listed" % token) if token else "no subtype declared"
                basis = "%s, so only sets common to %s are required" % (reason, label)
        required = set(pools[0])
        for pool in pools[1:]:
            required &= pool
        if not required and matched and len(pools) > 1:
            # Components sharing a token with no set in common (BCA splits
            # "Parking Lot" and "Parking Lot (relevant elements)") are one object.
            for pool in pools:
                required |= pool
            basis += "; the components share no set, so all of their sets are required"
        return sorted(required), basis

    def area_scheme_by_token(self, token):
        if not token:
            return None
        want = _norm(token).lstrip("*")
        for scheme in self.area_schemes():
            if _norm(scheme.get("object_type_token", "")).lstrip("*") == want:
                return scheme
            if _norm(scheme.get("subtype", "")) == want:
                return scheme
        return None

    def area_scheme_tokens(self):
        return [
            scheme.get("object_type_token")
            for scheme in self.area_schemes()
            if scheme.get("object_type_token")
        ]

    def _catalogue_domains(self):
        out = []
        for domain in self.catalogue.get("entity_domains", []):
            out.append((domain.get("domain") or "", domain.get("members") or []))
        return out

    def known_entities(self):
        out = set()
        for _label, members in self._catalogue_domains():
            for entity in members:
                out.add(entity if isinstance(entity, str) else entity.get("entity", ""))
        out.discard("")
        return out

    def domains_of(self, entity):
        if self._domain_index is None:
            index = {}
            for label, members in self._catalogue_domains():
                for item in members:
                    name = item if isinstance(item, str) else item.get("entity", "")
                    if not name:
                        continue
                    bucket = index.setdefault(name.upper(), [])
                    if label and label not in bucket:
                        bucket.append(label)
            self._domain_index = index
        return list(self._domain_index.get((entity or "").upper(), []))

    def domain_of(self, entity):
        return " / ".join(self.domains_of(entity))

    def domains(self):
        out = []
        for label, _members in self._catalogue_domains():
            if label and label not in out:
                out.append(label)
        return out

    def resolve_pset(self, name):
        if not name:
            return Resolution(UNKNOWN, name)
        if name in self.sgpsets:
            return Resolution(EXACT, name)
        canonical = self.sgpsets_by_norm.get(_norm(name))
        if canonical and canonical != name:
            status = CASE_MISMATCH if canonical.lower() == name.lower() else NEAR_MISS
            return Resolution(status, canonical, [canonical])
        close = difflib.get_close_matches(
            name, list(self.sgpsets.keys()), n=3, cutoff=0.82)
        if close:
            return Resolution(NEAR_MISS, close[0], close)
        return Resolution(UNKNOWN, name)

    def resolve_property(self, pset_name, prop_name):
        entry = self.sgpsets.get(pset_name)
        if entry is None:
            return Resolution(
                UNKNOWN, prop_name, [],
                "Property set '%s' is not in the loaded catalogue." % pset_name)
        names = [
            prop.get("name") for prop in entry.get("properties", [])
            if prop.get("name")
        ]
        if not names:
            return Resolution(
                UNKNOWN, prop_name, [],
                "Property set '%s' has no properties." % pset_name)
        if prop_name in names:
            return Resolution(EXACT, prop_name)
        lowered = dict((item.lower(), item) for item in names)
        if prop_name and prop_name.lower() in lowered:
            return Resolution(
                CASE_MISMATCH, lowered[prop_name.lower()],
                [lowered[prop_name.lower()]])
        normed = dict((_norm(item), item) for item in names)
        if _norm(prop_name) in normed:
            return Resolution(
                NEAR_MISS, normed[_norm(prop_name)], [normed[_norm(prop_name)]])
        close = difflib.get_close_matches(prop_name or "", names, n=3, cutoff=0.80)
        if close:
            return Resolution(NEAR_MISS, close[0], close)
        return Resolution(UNKNOWN, prop_name, [])

    def property_datatype(self, pset_name, prop_name):
        entry = self.sgpsets.get(pset_name)
        if entry:
            for prop in entry.get("properties", []):
                if prop.get("name") == prop_name:
                    return prop.get("type")
        return None

    def property_datatypes(self, pset_name, prop_name):
        """Mapping datatype plus any datatype the COP document gives instead."""
        entry = self.sgpsets.get(pset_name)
        if entry:
            for prop in entry.get("properties", []):
                if prop.get("name") == prop_name:
                    return [prop.get("type")] + list(prop.get("alt_types") or [])
        return []

    def property_is_controlled(self, pset_name, prop_name):
        entry = self.sgpsets.get(pset_name)
        if entry:
            for prop in entry.get("properties", []):
                if prop.get("name") == prop_name:
                    return bool(prop.get("controlled"))
        return False

    def required_properties(self, pset_name):
        entry = self.sgpsets.get(pset_name)
        if entry is None:
            return []
        return [
            prop.get("name") for prop in entry.get("properties", [])
            if prop.get("name")
        ]

    def psets_for_entity(self, ifc_entity):
        want = (ifc_entity or "").upper()
        return sorted(
            name for name, entry in self.sgpsets.items()
            if any(str(entity).upper() == want
                   for entity in entry.get("entities", [])))

    def owning_psets(self, prop_name):
        return sorted(self.property_owners.get(prop_name, set()))

    def disclaimer(self):
        return self.metadata.get(
            "disclaimer",
            "This is a pre-flight aid, not a compliance determination. "
            "The Qualified Person remains responsible for code compliance.")
