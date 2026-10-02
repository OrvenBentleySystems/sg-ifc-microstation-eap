"""Report assembly and export.

The verdict is an ERROR count. No score is published as a headline: a validator
score is a coverage estimate, not compliance.
"""

import csv
import datetime
import json
import os

try:
    from . import TOOL_NAME
except ImportError:
    TOOL_NAME = "IFC + SG Checker"

from .rules import FAIL, WARN, UNKNOWN, NOT_APPLICABLE, PASS, STATUS_ORDER

# Pictures in the HTML report: enough to review every object type, small enough
# that the file opens quickly. Remaining objects are listed without a picture.
MAX_PICTURES = 150
REPORT_SEGMENTS = 900
SEVERITY_RANK = {"ERROR": 0, "WARN": 1}


class Report(object):
    def __init__(self, source, ifc, context, results, library):
        self.source = source
        self.f = ifc
        self.ctx = context
        self.results = sorted(results, key=lambda r: (STATUS_ORDER.get(r.status, 9), r.rule_id))
        self.lib = library
        self.generated = datetime.datetime.now().replace(microsecond=0).isoformat()
        self._counts = None
        self._object_rows = None
        self._breakdown = None
        self._failing = None
        self._issue_index = None

    # -- objects to fix -----------------------------------------------------

    def issue_index(self):
        """{ifc id: [issue, ...]} for every object named by a FAIL or WARN rule."""
        if self._issue_index is not None:
            return self._issue_index
        index = {}
        for r in self.results:
            if r.status not in (FAIL, WARN):
                continue
            for f in r.findings:
                issue = {"severity": f.severity, "rule": r.rule_id, "title": r.title,
                         "message": f.message, "detail": f.detail, "fix": r.fix}
                for oid in f.all_ids:
                    bucket = index.setdefault(oid, [])
                    if not any(i["rule"] == issue["rule"] and i["message"] == issue["message"]
                               for i in bucket):
                        bucket.append(issue)
        for bucket in index.values():
            bucket.sort(key=lambda i: (SEVERITY_RANK.get(i["severity"], 9), i["rule"]))
        self._issue_index = index
        return index

    def describe_object(self, oid):
        ent = self.f.get(oid)
        if ent is None:
            return {"id": oid, "entity": "?", "name": "", "guid": "", "storey": ""}
        return {"id": oid, "entity": ent.type, "name": self.ctx.label(ent),
                "guid": self.ctx.guid(ent), "storey": self.ctx.storey_label(oid)}

    def failing_objects(self):
        """One row per object to fix: errors first, then most issues, then class."""
        if self._failing is not None:
            return self._failing
        rows = []
        for oid, issues in self.issue_index().items():
            row = self.describe_object(oid)
            row["issues"] = issues
            row["severity"] = issues[0]["severity"] if issues else "WARN"
            rows.append(row)
        rows.sort(key=lambda r: (SEVERITY_RANK.get(r["severity"], 9), -len(r["issues"]),
                                 r["entity"], r["id"]))
        self._failing = rows
        return rows

    def picture_targets(self, limit=MAX_PICTURES):
        return [row["id"] for row in self.failing_objects()[:limit]]

    # -- verdict ------------------------------------------------------------

    @property
    def counts(self):
        if self._counts is not None:
            return self._counts
        c = {FAIL: 0, WARN: 0, UNKNOWN: 0, NOT_APPLICABLE: 0, PASS: 0}
        for r in self.results:
            c[r.status] = c.get(r.status, 0) + 1
        self._counts = c
        return c

    @property
    def error_findings(self):
        return sum(1 for r in self.results for f in r.findings if f.severity == "ERROR")

    @property
    def warn_findings(self):
        return sum(1 for r in self.results for f in r.findings if f.severity == "WARN")

    @property
    def verdict(self):
        c = self.counts
        if c[FAIL]:
            return "NOT READY - %d rule(s) failed, %d element-level errors" % (
                c[FAIL], self.error_findings)
        if c[UNKNOWN]:
            return "INCONCLUSIVE - %d rule(s) could not be evaluated" % c[UNKNOWN]
        if c[WARN]:
            return "PASSED WITH %d WARNING RULE(S) - QP judgement required" % c[WARN]
        return "ALL EVALUATED RULES PASSED"

    def object_checklist(self):
        """Per IFC class: how many objects conform, and on which rules they fail."""
        if self._object_rows is not None:
            return self._object_rows
        per_class = {}
        for ent in self.ctx.element_ids + self.f.of_type("IFCSPACE"):
            row = per_class.setdefault(ent.type, {"total": 0, "failing": set(), "rules": {}})
            row["total"] += 1
        for r in self.results:
            if r.status not in (FAIL, WARN):
                continue
            for fnd in r.findings:
                for oid in fnd.all_ids:
                    entity = self.f.type_of(oid)
                    row = per_class.get(entity)
                    if row is None:
                        continue
                    if fnd.severity == "ERROR":
                        row["failing"].add(oid)
                    row["rules"].setdefault(r.rule_id, set()).add(oid)
        out = []
        for cls in sorted(per_class):
            row = per_class[cls]
            failing = len(row["failing"])
            out.append({
                "entity": cls,
                "total": row["total"],
                "conforming": row["total"] - failing,
                "failing": failing,
                "rules": ", ".join("%s(%d)" % (rid, len(ids))
                                   for rid, ids in sorted(row["rules"].items())),
            })
        self._object_rows = out
        return out

    # -- export -------------------------------------------------------------

    def breakdown(self):
        """Elements grouped discipline -> storey -> IFC class, with failure counts."""
        if self._breakdown is not None:
            return self._breakdown
        failing = set()
        touched = {}
        for r in self.results:
            if r.status not in (FAIL, WARN):
                continue
            for fnd in r.findings:
                for oid in fnd.all_ids:
                    touched.setdefault(oid, set()).add(r.rule_id)
                    if fnd.severity == "ERROR":
                        failing.add(oid)
        tree = {}
        for ent in self.ctx.element_ids + self.f.of_type("IFCSPACE"):
            disc = self.lib.domain_of(ent.type) or "UNCATALOGUED"
            storey = self.ctx.storey_label(ent.id)
            node = tree.setdefault(disc, {}).setdefault(storey, {}).setdefault(
                ent.type, {"ids": [], "failing": [], "rules": set()})
            node["ids"].append(ent.id)
            if ent.id in failing:
                node["failing"].append(ent.id)
            node["rules"].update(touched.get(ent.id, ()))
        self._breakdown = tree
        return tree

    def breakdown_rows(self):
        """Flattened breakdown, one row per discipline/storey/class combination."""
        rows = []
        tree = self.breakdown()
        for disc in sorted(tree):
            for storey in sorted(tree[disc]):
                for cls in sorted(tree[disc][storey]):
                    node = tree[disc][storey][cls]
                    rows.append({
                        "discipline": disc,
                        "storey": storey,
                        "entity": cls,
                        "total": len(node["ids"]),
                        "failing": len(node["failing"]),
                        "conforming": len(node["ids"]) - len(node["failing"]),
                        "rules": ", ".join(sorted(node["rules"])),
                    })
        return rows

    def header(self):
        ed = self.lib.editions
        return {
            "generated": self.generated,
            "tool": TOOL_NAME,
            "source_kind": self.source.kind,
            "source_label": self.source.label,
            "source_path": self.source.path,
            "ifc_schema": self.f.schema,
            "ifc_view_definition": "; ".join(self.f.view_definitions),
            "originating_system": self.f.originating_system,
            "ifc_timestamp": self.f.timestamp,
            "entity_count": self.f.count(),
            "element_count": len(self.ctx.element_ids),
            "mapping_edition": ed["mapping_edition"],
            "cop_edition": ed["cop_edition"],
            "cop_published": ed.get("cop_published", ""),
            "cop_superseded": bool(ed.get("superseded")),
            "library_source": ed["sgpset_source"],
            "library_sgpset_count": ed["sgpset_count"],
            "library_untranscribed": ed["untranscribed_count"],
            "library_complete": self.lib.is_complete,
            "library_built_from": ed.get("source_files", "-"),
            "library_built_at": ed.get("built_at", ""),
            "library_mapping_age_days": ed.get("mapping_age_days"),
            "verdict": self.verdict,
            "rules_failed": self.counts[FAIL],
            "rules_warned": self.counts[WARN],
            "rules_unknown": self.counts[UNKNOWN],
            "rules_passed": self.counts[PASS],
            "element_errors": self.error_findings,
            "disclaimer": self.lib.disclaimer(),
        }

    def as_dict(self):
        return {
            "header": self.header(),
            "objects_to_fix": self.failing_objects(),
            "object_checklist": self.object_checklist(),
            "breakdown": self.breakdown_rows(),
            "rules": [r.as_dict() for r in self.results],
        }

    def write_json(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, indent=2, ensure_ascii=False)
        return path

    def write_csv(self, path):
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Rule", "Status", "DeclaredSeverity", "Category", "Title",
                        "FindingSeverity", "IfcId", "Entity", "Name", "GlobalId",
                        "Message", "Detail"])
            for r in self.results:
                if not r.findings:
                    w.writerow([r.rule_id, r.status, r.declared_severity, r.category,
                                r.title, "", "", "", "", "", r.summary, ""])
                    continue
                for f in r.findings:
                    w.writerow([r.rule_id, r.status, r.declared_severity, r.category,
                                r.title, f.severity,
                                "#%s" % f.ifc_id if f.ifc_id is not None else "",
                                f.entity, f.name, f.guid, f.message, f.detail])
        return path

    def write_text(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.as_text())
        return path

    def write_html(self, path, pictures=None):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.as_html(pictures))
        return path

    def _objects_section(self, pictures):
        """Every object to fix, with its picture and every reason, errors first."""
        e = _esc
        rows = self.failing_objects()
        if not rows:
            return ""
        pictures = pictures or {}
        errors = sum(1 for r in rows if r["severity"] == "ERROR")
        out = ['<section id="fix"><h2>Objects to fix</h2>',
               '<p class="lead">%d object(s) need attention: %d with errors, %d with '
               'warnings only. Each card shows the object and every reason it does not '
               'follow the loaded COP mapping.%s</p>'
               % (len(rows), errors, len(rows) - errors,
                  "" if len(rows) <= MAX_PICTURES else
                  " The first %d are shown as cards; the rest are listed below."
                  % MAX_PICTURES),
               '<div class="objgrid">']
        for row in rows[:MAX_PICTURES]:
            pic = pictures.get(row["id"])
            sev = "bad" if row["severity"] == "ERROR" else "warn"
            out.append('<article class="obj %s" id="obj-%s">' % (sev, row["id"]))
            out.append('<div class="pic">%s</div>' % (
                pic or '<div class="nopic">No picture: geometry is not available for '
                       'this object.</div>'))
            out.append('<div class="objbody"><h4>%s <span class="mono">#%s</span></h4>'
                       '<div class="meta">%s</div>'
                       % (e(row["name"] or "(unnamed)"), row["id"],
                          e("%s  \u00b7  storey %s  \u00b7  GlobalId %s"
                            % (row["entity"], row["storey"], row["guid"] or "-"))))
            out.append('<ul class="issues">')
            for issue in row["issues"][:8]:
                out.append('<li class="%s"><b>%s %s</b> %s%s%s</li>' % (
                    "bad" if issue["severity"] == "ERROR" else "warn",
                    e(issue["severity"]), e(issue["rule"]), e(issue["message"]),
                    ('<span class="det">%s</span>' % e(issue["detail"]))
                    if issue["detail"] else "",
                    ('<span class="howto">Fix: %s</span>' % e(issue["fix"]))
                    if issue["fix"] else ""))
            if len(row["issues"]) > 8:
                out.append('<li class="more">+ %d more issue(s) in the rule checklist '
                           'below and in the CSV export.</li>' % (len(row["issues"]) - 8))
            out.append('</ul></div></article>')
        out.append('</div>')
        rest = rows[MAX_PICTURES:]
        if rest:
            out.append('<table class="grid"><thead><tr><th>IFC id</th><th>Entity</th>'
                       '<th>Name</th><th>Storey</th><th>GlobalId</th><th class="num">Issues</th>'
                       '<th>Reasons</th></tr></thead><tbody>')
            for row in rest:
                reasons = "; ".join("%s %s" % (i["rule"], i["message"]) for i in row["issues"])
                out.append('<tr class="%s"><td class="mono">#%s</td><td class="mono">%s</td>'
                           '<td>%s</td><td>%s</td><td class="mono">%s</td>'
                           '<td class="num">%d</td><td>%s</td></tr>'
                           % ("bad" if row["severity"] == "ERROR" else "warn", row["id"],
                              e(row["entity"]), e(row["name"]), e(row["storey"]),
                              e(row["guid"]), len(row["issues"]), e(reasons)))
            out.append('</tbody></table>')
        out.append('</section>')
        return "\n".join(out)

    def as_html(self, pictures=None):
        h = self.header()
        e = _esc
        parts = [_HTML_HEAD % e(os.path.basename(h["source_path"] or "IFC+SG check"))]

        verdict_class = ("v-fail" if self.counts[FAIL]
                         else "v-unknown" if self.counts[UNKNOWN]
                         else "v-warn" if self.counts[WARN] else "v-pass")

        parts.append('<header class="top">')
        parts.append('<h1>%s</h1>' % e(TOOL_NAME))
        parts.append('<div class="file">%s</div>' % e(h["source_path"] or ""))
        parts.append('<div class="verdict %s">%s</div>' % (verdict_class, e(h["verdict"])))
        parts.append('<div class="counts">')
        for label, key, cls in (("Rules failed", "rules_failed", "c-fail"),
                                ("Warning rules", "rules_warned", "c-warn"),
                                ("Unevaluated", "rules_unknown", "c-unknown"),
                                ("Rules passed", "rules_passed", "c-pass"),
                                ("Object errors", "element_errors", "c-fail"),
                                ("Objects checked", "element_count", "c-plain")):
            parts.append('<div class="count %s"><b>%s</b><span>%s</span></div>'
                         % (cls, h[key], e(label)))
        parts.append('</div></header>')

        # -- provenance ------------------------------------------------------
        stale = h.get("library_mapping_age_days")
        stale_cls = " class=\"warn-cell\"" if (stale is not None and stale > 365) else ""
        parts.append('<section id="provenance"><h2>Provenance</h2>')
        parts.append('<div class="cards">')
        parts.append(_card("Model", [
            ("IFC schema", h["ifc_schema"]),
            ("Model view definition", h["ifc_view_definition"]),
            ("Authoring system", h["originating_system"]),
            ("Exported", h["ifc_timestamp"]),
            ("Entities / objects", "%s / %s" % (h["entity_count"], h["element_count"])),
        ]))
        parts.append(_card("Rule catalogue", [
            ("Mapping edition", h["mapping_edition"], stale_cls),
            ("Mapping age", "%s days" % stale if stale is not None else "unknown", stale_cls),
            ("COP edition", "%s%s%s" % (
                h["cop_edition"],
                " (%s)" % h["cop_published"] if h.get("cop_published") else "",
                " - superseded" if h.get("cop_superseded") else "")),
            ("Source", h["library_source"]),
            ("Built from", h["library_built_from"]),
            ("Property sets", h["library_sgpset_count"]),
        ]))
        parts.append(_card("Report", [
            ("Generated", h["generated"]),
            ("Tool", h["tool"]),
            ("Rules evaluated", len(self.results)),
        ]))
        parts.append('</div></section>')

        parts.append(self._objects_section(pictures))

        # -- object checklist -------------------------------------------------
        checklist = self.object_checklist()
        parts.append('<section id="objects"><h2>Object checklist</h2>')
        parts.append('<table class="grid objects"><thead><tr>'
                     '<th>IFC class</th><th class="num">Objects</th>'
                     '<th class="num">Conforming</th><th class="num">Failing</th>'
                     '<th>Conformance</th><th>Failing rules</th>'
                     '</tr></thead><tbody>')
        for row in checklist:
            total = row["total"] or 1
            pct = 100.0 * row["conforming"] / total
            cls = "ok" if row["failing"] == 0 else "bad"
            bar = ('<div class="bar"><span style="width:%.1f%%" class="%s"></span></div>'
                   '<em>%.0f%%</em>' % (pct, "b-ok" if pct == 100 else "b-bad", pct))
            parts.append('<tr class="%s"><td class="mono">%s</td><td class="num">%d</td>'
                         '<td class="num">%d</td><td class="num">%d</td><td>%s</td>'
                         '<td class="rules">%s</td></tr>'
                         % (cls, e(row["entity"]), row["total"], row["conforming"],
                            row["failing"], bar, e(row["rules"])))
        parts.append('</tbody></table></section>')

        # -- rules ------------------------------------------------------------
        entities = sorted({f.entity for r in self.results for f in r.findings if f.entity})
        parts.append('<section id="rules"><h2>Rule checklist</h2>')
        parts.append(_controls(entities))

        for r in self.results:
            rule_entities = sorted({f.entity for f in r.findings if f.entity})
            errors = sum(1 for f in r.findings if f.severity == "ERROR")
            warns = sum(1 for f in r.findings if f.severity == "WARN")
            open_attr = " open" if r.status == FAIL and len(r.findings) <= 60 else ""
            anchor = r.rule_id.replace(".", "-")
            parts.append('<details class="rule" id="%s" data-status="%s" data-entities="%s"%s>'
                         % (anchor, r.status, e(" ".join(rule_entities)), open_attr))
            badges = ""
            if errors:
                badges += '<span class="badge b-err">%d error</span>' % errors
            if warns:
                badges += '<span class="badge b-warn">%d warning</span>' % warns
            parts.append('<summary><span class="pill p-%s">%s</span>'
                         '<span class="rid">%s</span><span class="rtitle">%s</span>%s</summary>'
                         % (r.status, r.status, e(r.rule_id), e(r.title), badges))
            parts.append('<div class="body">')
            parts.append('<p class="summary">%s</p>' % e(r.summary))
            if r.fix and r.status in (FAIL, WARN):
                parts.append('<p class="fix"><b>Fix</b> %s</p>' % e(r.fix))
            if r.findings:
                parts.append('<table class="grid findings"><thead><tr>'
                             '<th>Severity</th><th>IFC id</th><th>Entity</th><th>Name</th>'
                             '<th>Finding</th><th class="num">Objects</th>'
                             '</tr></thead><tbody>')
                for f in r.findings:
                    sev = "bad" if f.severity == "ERROR" else "warn"
                    detail = f.message + (("  \u2014  " + f.detail) if f.detail else "")
                    parts.append(
                        '<tr class="%s" data-sev="%s" data-entity="%s">'
                        '<td class="sev">%s</td><td class="mono">%s</td><td class="mono">%s</td>'
                        '<td>%s</td><td>%s</td><td class="num">%d</td></tr>'
                        % (sev, e(f.severity), e(f.entity), e(f.severity),
                           ("#%s" % f.ifc_id) if f.ifc_id is not None else "",
                           e(f.entity), e(f.name), e(detail), len(f.all_ids)))
                parts.append('</tbody></table>')
            parts.append('</div></details>')
        parts.append('</section>')

        parts.append('<p class="disclaimer"><b>Not a compliance determination.</b> %s</p>'
                     % e(h["disclaimer"]))
        parts.append('<a href="#" class="totop" title="Back to top">&#8679;</a>')
        parts.append(_HTML_TAIL)
        return "\n".join(parts)

    def as_text(self):
        h = self.header()
        lines = []
        lines.append(TOOL_NAME)
        lines.append("=" * 68)
        for key in ("generated", "source_label", "source_path", "ifc_schema",
                    "ifc_view_definition", "originating_system", "entity_count",
                    "element_count", "mapping_edition", "cop_edition",
                    "library_source", "library_sgpset_count", "library_untranscribed"):
            lines.append("%-22s %s" % (key + ":", h[key]))
        lines.append("")
        lines.append("VERDICT: %s" % h["verdict"])
        lines.append("")
        lines.append("Object checklist")
        lines.append("-" * 68)
        lines.append("%-28s %7s %11s %8s  %s" % ("IFC class", "total", "conforming",
                                                 "failing", "failing rules"))
        for row in self.object_checklist():
            lines.append("%-28s %7d %11d %8d  %s" % (
                row["entity"], row["total"], row["conforming"], row["failing"], row["rules"]))
        lines.append("")
        lines.append("Rule checklist")
        lines.append("-" * 68)
        for r in self.results:
            lines.append("[%-14s] %-12s %s" % (r.status, r.rule_id, r.title))
            lines.append("    %s" % r.summary)
            if r.findings:
                lines.append("    %d finding(s); first 5:" % len(r.findings))
                for f in r.findings[:5]:
                    tag = "#%s %s %s" % (f.ifc_id, f.entity, f.name) if f.ifc_id else ""
                    lines.append("      - %s %s" % (tag, f.message))
            lines.append("")
        lines.append(h["disclaimer"])
        return "\n".join(lines)


def default_export_path(source_path, extension):
    base = os.path.splitext(os.path.basename(source_path or "ifcsg"))[0]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = os.path.dirname(source_path) if source_path else os.path.expanduser("~")
    if not os.path.isdir(folder):
        folder = os.path.expanduser("~")
    return os.path.join(folder, "%s_ifcsg-check_%s.%s" % (base, stamp, extension))


def _esc(value):
    if value is None:
        return ""
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _card(title, rows):
    out = ['<div class="card"><h3>%s</h3><table>' % _esc(title)]
    for row in rows:
        label, value = row[0], row[1]
        extra = row[2] if len(row) > 2 else ""
        out.append("<tr><th>%s</th><td%s>%s</td></tr>"
                   % (_esc(label), extra, _esc(value if value not in (None, "") else "-")))
    out.append("</table></div>")
    return "".join(out)


def _controls(entities):
    options = "".join('<option value="%s">%s</option>' % (_esc(x), _esc(x)) for x in entities)
    return """<div class="controls">
<input type="search" id="q" placeholder="Filter rules and findings...">
<label><input type="checkbox" class="st" value="FAIL" checked> Fail</label>
<label><input type="checkbox" class="st" value="WARN" checked> Warn</label>
<label><input type="checkbox" class="st" value="UNKNOWN" checked> Unevaluated</label>
<label><input type="checkbox" class="st" value="PASS" checked> Pass</label>
<label><input type="checkbox" class="st" value="NOT_APPLICABLE" checked> N/A</label>
<select id="ent"><option value="">All IFC classes</option>%s</select>
<button type="button" id="expand">Expand</button>
<button type="button" id="collapse">Collapse</button>
<span id="shown" class="shown"></span>
</div>""" % options


_HTML_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IFC+SG check - %s</title>
<style>
:root{
  --fail:#c4314b; --warn:#b26a00; --unknown:#6b4fbb; --pass:#13804a;
  --line:#dde2ea; --bg:#f3f5f8; --ink:#1b2330; --muted:#667085;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{font:14px/1.55 "Segoe UI",system-ui,-apple-system,sans-serif;margin:0;
     padding:0 32px 64px;color:var(--ink);background:var(--bg)}
h1{font-size:23px;margin:0 0 2px;font-weight:650}
h1 .sub{font-size:13px;color:var(--muted);font-weight:400;margin-left:8px}
h2{font-size:15px;margin:34px 0 10px;text-transform:uppercase;letter-spacing:.06em;
   color:var(--muted);font-weight:650}
h3{font-size:12px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
header.top{padding:26px 0 4px}
header.top .file{font-family:Consolas,monospace;font-size:12px;color:var(--muted);
                 margin-bottom:14px;word-break:break-all}
.verdict{padding:13px 18px;border-radius:7px;font-weight:650;color:#fff;font-size:15px}
.v-fail{background:var(--fail)}.v-warn{background:var(--warn)}
.v-unknown{background:var(--unknown)}.v-pass{background:var(--pass)}
.counts{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.count{background:#fff;border:1px solid var(--line);border-radius:7px;padding:9px 16px;min-width:112px}
.count b{display:block;font-size:21px;line-height:1.2}
.count span{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.c-fail b{color:var(--fail)}.c-warn b{color:var(--warn)}
.c-unknown b{color:var(--unknown)}.c-pass b{color:var(--pass)}
.cards{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-start}
.card{background:#fff;border:1px solid var(--line);border-radius:7px;padding:14px 16px;
      flex:1 1 300px;min-width:280px}
.card table{width:100%%;border:0}
.card th,.card td{border:0;padding:2px 0;font-size:13px;vertical-align:top}
.card th{text-align:left;color:var(--muted);font-weight:400;width:46%%}
.card td{word-break:break-word}
td.warn-cell{color:var(--warn);font-weight:650}
table{border-collapse:collapse;width:100%%;background:#fff}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;vertical-align:top}
thead th{background:#eef0f2;font-weight:650;position:sticky;top:0;z-index:2;font-size:12.5px}
td.num,th.num{text-align:right;white-space:nowrap}
td.mono{font-family:Consolas,monospace;font-size:12px;white-space:nowrap}
td.sev{font-size:11px;font-weight:650;white-space:nowrap}
td.rules{font-family:Consolas,monospace;font-size:11.5px;color:var(--muted)}
tbody tr:nth-child(even){background:#fbfbfc}
tr.ok td:first-child{border-left:3px solid var(--pass)}
tr.bad td:first-child{border-left:3px solid var(--fail)}
tr.bad td.sev{color:var(--fail)}
tr.warn td:first-child{border-left:3px solid var(--warn)}
tr.warn td.sev{color:var(--warn)}
.bar{display:inline-block;width:110px;height:8px;background:#e6e8ea;border-radius:4px;
     overflow:hidden;vertical-align:middle}
.bar span{display:block;height:100%%}
.b-ok{background:var(--pass)}.b-bad{background:var(--fail)}
.objects em{font-style:normal;font-size:11.5px;color:var(--muted);margin-left:8px}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:#fff;
          border:1px solid var(--line);border-radius:7px;padding:10px 14px;margin-bottom:12px;
          position:sticky;top:0;z-index:5;box-shadow:0 1px 4px rgba(0,0,0,.06)}
.controls input[type=search]{padding:6px 9px;border:1px solid var(--line);border-radius:5px;
                             min-width:230px;font:inherit}
.controls select{padding:5px 8px;border:1px solid var(--line);border-radius:5px;font:inherit}
.controls label{font-size:13px;cursor:pointer;user-select:none}
.controls button{font:inherit;padding:5px 12px;border:1px solid var(--line);background:#f2f3f5;
                 border-radius:5px;cursor:pointer}
.controls button:hover{background:#e7e9ec}
.shown{margin-left:auto;font-size:12px;color:var(--muted)}
details.rule{background:#fff;border:1px solid var(--line);border-radius:7px;margin:8px 0}
details.rule[hidden]{display:none}
summary{cursor:pointer;padding:10px 14px;display:flex;align-items:center;gap:11px;
        user-select:none;list-style:none}
summary::-webkit-details-marker{display:none}
summary:hover{background:#fafbfc}
.pill{font-size:10.5px;font-weight:700;letter-spacing:.05em;padding:3px 9px;border-radius:11px;
      color:#fff;min-width:74px;text-align:center}
.p-FAIL{background:var(--fail)}.p-WARN{background:var(--warn)}
.p-UNKNOWN{background:var(--unknown)}.p-PASS{background:var(--pass)}
.p-NOT_APPLICABLE{background:#7a7f85}
.rid{font-family:Consolas,monospace;font-weight:700;min-width:104px;font-size:12.5px}
.rtitle{flex:1}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;margin-left:4px;white-space:nowrap}
.b-err{background:#fdecea;color:var(--fail)}
.b-warn{background:#fdf3e2;color:var(--warn)}
.body{padding:0 14px 14px}
p.summary{margin:0 0 10px;color:#2b2f33}
p.fix{margin:0 0 12px;padding:8px 11px;background:#f4f6f8;border-left:3px solid var(--unknown);
      font-size:13px;color:#33383d}
p.fix b{text-transform:uppercase;font-size:11px;letter-spacing:.05em;color:var(--unknown);
        margin-right:6px}
table.findings thead th{top:52px}
p.disclaimer{margin-top:34px;padding:14px 16px;background:#fff;
             border-left:4px solid var(--unknown);color:#41464b;font-size:13px;border-radius:0 7px 7px 0}
.totop{position:fixed;right:22px;bottom:22px;width:38px;height:38px;border-radius:19px;
       background:#fff;border:1px solid var(--line);color:var(--muted);text-decoration:none;
       display:flex;align-items:center;justify-content:center;font-size:17px;
       box-shadow:0 2px 6px rgba(0,0,0,.12)}
p.lead{margin:0 0 12px;color:#344054}
.objgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(560px,1fr));gap:12px;
         margin-bottom:14px}
.obj{display:flex;gap:12px;background:#fff;border:1px solid var(--line);border-radius:7px;
     padding:10px;break-inside:avoid}
.obj.bad{border-left:4px solid var(--fail)}.obj.warn{border-left:4px solid var(--warn)}
.obj .pic{flex:0 0 240px;height:170px;border:1px solid #eef1f5;border-radius:5px;
          display:flex;align-items:center;justify-content:center;overflow:hidden;background:#fff}
.obj .pic svg{width:240px;height:170px}
.nopic{font-size:12px;color:var(--muted);padding:12px;text-align:center}
.objbody{flex:1;min-width:0}
.objbody h4{margin:0 0 2px;font-size:14px}
.objbody .meta{font-size:11.5px;color:var(--muted);margin-bottom:6px;word-break:break-all}
ul.issues{margin:0;padding-left:16px;font-size:12.5px}
ul.issues li{margin:3px 0}
ul.issues li.bad b{color:var(--fail)}ul.issues li.warn b{color:var(--warn)}
ul.issues .det{display:block;color:var(--muted);font-size:11.5px}
ul.issues .howto{display:block;color:#344054;font-size:11.5px}
@media print{
  body{padding:0;background:#fff;font-size:11px}
  .controls,.totop{display:none}
  details.rule{break-inside:avoid;border-color:#bbb}
  details.rule>.body{display:block !important}
  details.rule[hidden]{display:block !important}
  thead th{position:static}
  .verdict{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
</style></head><body>
"""

_HTML_TAIL = """<script>
(function(){
  var rules  = Array.prototype.slice.call(document.querySelectorAll('details.rule'));
  var q      = document.getElementById('q');
  var ent    = document.getElementById('ent');
  var boxes  = Array.prototype.slice.call(document.querySelectorAll('input.st'));
  var shown  = document.getElementById('shown');

  function apply(){
    var text = q.value.trim().toLowerCase();
    var wantEntity = ent.value;
    var on = {};
    boxes.forEach(function(b){ on[b.value] = b.checked; });
    var visible = 0;

    rules.forEach(function(rule){
      var statusOk = on[rule.getAttribute('data-status')];
      var entityOk = !wantEntity ||
        (' ' + rule.getAttribute('data-entities') + ' ').indexOf(' ' + wantEntity + ' ') !== -1;
      var textOk = !text || rule.textContent.toLowerCase().indexOf(text) !== -1;
      var show = statusOk && entityOk && textOk;
      rule.hidden = !show;
      if (show) {
        visible++;
        if (text || wantEntity) { rule.open = true; }
        rule.querySelectorAll('tbody tr').forEach(function(row){
          var rowEntity = row.getAttribute('data-entity') || '';
          var rowOk = (!wantEntity || rowEntity === wantEntity) &&
                      (!text || row.textContent.toLowerCase().indexOf(text) !== -1);
          row.hidden = !rowOk;
        });
      }
    });
    shown.textContent = visible + ' of ' + rules.length + ' rules shown';
  }

  q.addEventListener('input', apply);
  ent.addEventListener('change', apply);
  boxes.forEach(function(b){ b.addEventListener('change', apply); });
  document.getElementById('expand').addEventListener('click', function(){
    rules.forEach(function(r){ if(!r.hidden) r.open = true; });
  });
  document.getElementById('collapse').addEventListener('click', function(){
    rules.forEach(function(r){ r.open = false; });
  });
  apply();
})();
</script></body></html>
"""
