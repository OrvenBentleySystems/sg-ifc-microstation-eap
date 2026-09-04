"""BCF 2.1 export.

One topic per failing or warning rule. Each topic carries a viewpoint that
selects the offending elements by IfcGuid, so opening the issue in any
BCF-capable coordination tool lands on the right objects.

Standard library only: zipfile plus ElementTree.
"""

import datetime
import uuid
import zipfile
import xml.etree.ElementTree as ET

from .rules import FAIL, WARN

BCF_VERSION = "2.1"

# A viewpoint listing every element of a large failing class is unusable and
# bloats the archive; the topic text still reports the true total.
MAX_COMPONENTS = 500
MAX_FINDINGS_IN_COMMENT = 500

PRIORITY = {"ERROR": "High", "WARN": "Normal"}


def _iso(dt=None):
    return (dt or datetime.datetime.now()).replace(microsecond=0).isoformat()


def _sub(parent, tag, text=None, **attrs):
    node = ET.SubElement(parent, tag, dict((k, v) for k, v in attrs.items() if v is not None))
    if text is not None:
        node.text = str(text)
    return node


def _document(root):
    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + body).encode("utf-8")


def _version_document():
    root = ET.Element("Version", {"VersionId": BCF_VERSION})
    _sub(root, "DetailedVersion", BCF_VERSION)
    return _document(root)


def _guids_for(report, result):
    """IfcGuids of every element the rule's findings point at, in file order."""
    seen = []
    known = set()
    for finding in result.findings:
        for oid in finding.all_ids:
            if oid in known:
                continue
            known.add(oid)
            ent = report.f.get(oid)
            if ent is None:
                continue
            guid = report.ctx.guid(ent)
            if guid:
                seen.append(guid)
    return seen


def _topic_body(result):
    lines = [result.summary or ""]
    for finding in result.findings[:MAX_FINDINGS_IN_COMMENT]:
        label = finding.name or finding.entity or "#%s" % finding.ifc_id
        lines.append("[%s] %s - %s" % (finding.severity, label, finding.message))
        if finding.detail:
            lines.append("    %s" % finding.detail)
    omitted = len(result.findings) - MAX_FINDINGS_IN_COMMENT
    if omitted > 0:
        lines.append("... %d more finding(s) omitted from this BCF comment; "
                     "the full checker export retains them." % omitted)
    return "\n".join(x for x in lines if x)


def _markup_document(report, result, topic_guid, viewpoint_guid, author, total):
    root = ET.Element("Markup")

    header = _sub(root, "Header")
    filenode = _sub(header, "File", IfcProject=None, isExternal="true")
    _sub(filenode, "Filename", report.source.label or "model.ifc")
    _sub(filenode, "Date", _iso())
    if report.source.path:
        _sub(filenode, "Reference", report.source.path)

    severity = "ERROR" if any(f.severity == "ERROR" for f in result.findings) else "WARN"
    topic = _sub(root, "Topic", Guid=topic_guid, TopicType="Issue", TopicStatus="Open")
    _sub(topic, "Title", "%s - %s" % (result.rule_id, result.title))
    _sub(topic, "Priority", PRIORITY.get(severity, "Normal"))
    _sub(topic, "CreationDate", _iso())
    _sub(topic, "CreationAuthor", author)
    _sub(topic, "Description", result.summary or result.title)
    labels = [result.category, result.status]
    for label in labels:
        if label:
            _sub(topic, "Labels", label)

    comment = _sub(root, "Comment", Guid=str(uuid.uuid4()))
    _sub(comment, "Date", _iso())
    _sub(comment, "Author", author)
    body = _topic_body(result)
    if total > MAX_COMPONENTS:
        body += ("\n\nViewpoint selects the first %d of %d elements."
                 % (MAX_COMPONENTS, total))
    _sub(comment, "Comment", body)

    if viewpoint_guid:
        viewpoints = _sub(root, "Viewpoints", Guid=viewpoint_guid)
        _sub(viewpoints, "Viewpoint", "viewpoint.bcfv")

    return _document(root)


def _viewpoint_document(guids, viewpoint_guid):
    root = ET.Element("VisualizationInfo", {"Guid": viewpoint_guid})
    components = _sub(root, "Components")
    selection = _sub(components, "Selection")
    for guid in guids[:MAX_COMPONENTS]:
        _sub(selection, "Component", IfcGuid=guid)
    _sub(components, "Visibility", DefaultVisibility="true")
    return _document(root)


def write_bcf(report, path, statuses=(FAIL, WARN), author="IFC+SG Checker"):
    """Write a BCF 2.1 archive. Returns (path, topic_count)."""
    topics = [r for r in report.results if r.status in statuses and r.findings]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bcf.version", _version_document())
        for result in topics:
            topic_guid = str(uuid.uuid4())
            guids = _guids_for(report, result)
            viewpoint_guid = str(uuid.uuid4()) if guids else ""
            zf.writestr("%s/markup.bcf" % topic_guid,
                        _markup_document(report, result, topic_guid, viewpoint_guid,
                                         author, len(guids)))
            if guids:
                zf.writestr("%s/viewpoint.bcfv" % topic_guid,
                            _viewpoint_document(guids, viewpoint_guid))
    return path, len(topics)
