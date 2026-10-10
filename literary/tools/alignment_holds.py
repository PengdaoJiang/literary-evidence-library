"""Fail-closed, append-only review holds; this module never authors a judgment.

The head inventories every immutable event byte string. A crash after the event
write is visible as an error until explicit recovery validates the whole chain.
Resolution is conditional on a caller-supplied release selection, not current.json.
"""
from __future__ import annotations

import copy
import json
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from literary.tools import alignment as a, alignment_release as r, alignment_review as review
from literary.tools import alignment_transaction as transaction


EVENT_NAME = re.compile(r"(\d{12})-([0-9a-f]{64})\.json\Z")
OPEN_FIELDS = {"event_type", "hold_id", "reporter_task_id", "reason", "chapter_ids",
               "observation_ids", "evidence_refs", "discovery_sha256", "classification"}
RESOLVE_FIELDS = {"event_type", "hold_id", "reviewer_task_id", "reason", "release_sha256",
                  "issue_locks", "stage_locks", "resolution"}
SEALED_FIELDS = {"schema_version", "record_type", "sequence", "previous_event_sha256",
                 "source_sha256", "input_manifest_sha256", "created_utc", "record_sha256"}


def _ids(value, label, *, nonempty=False):
    a._require(isinstance(value, list) and all(isinstance(key, str) and key.strip() for key in value),
               label + " must be a list of non-empty IDs")
    a._require(len(value) == len(set(value)) and (value or not nonempty), label + " must be unique and non-empty where required")
    return set(value)


def _json(path):
    a._require(path.is_file() and not path.is_symlink(), "hold member is not a regular file: " + str(path))
    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=a._unique_object)
    except (ValueError, UnicodeError, OSError) as exc:
        raise a.AlignmentError("invalid hold JSON: " + str(path)) from exc
    a._require(isinstance(value, dict), "hold JSON must be an object")
    return value, payload


def _pins(context):
    return {"source_sha256": context.manifest["source"]["sha256"],
            "input_manifest_sha256": context.manifest_file_sha256}


def _shape(context, event, *, sealed=False):
    a._require(isinstance(event, dict), "hold event must be an object")
    kind = event.get("event_type")
    a._require(isinstance(kind, str) and kind in {"open", "resolve"}, "unknown hold event type")
    expected = OPEN_FIELDS if kind == "open" else RESOLVE_FIELDS
    if sealed:
        expected = expected | SEALED_FIELDS | ({"opened_record_sha256"} if kind == "resolve" else set())
    a._require(set(event) == expected, "unexpected or missing hold event fields")
    for field in ("hold_id", "reason", "reporter_task_id" if kind == "open" else "reviewer_task_id"):
        a._require(isinstance(event[field], str) and bool(event[field].strip()), "hold requires " + field)
    if kind == "open":
        chapters = _ids(event["chapter_ids"], "hold chapter_ids", nonempty=True)
        a._require(chapters <= set(context.chapter_by_id), "hold has unknown chapter IDs")
        _ids(event["observation_ids"], "hold observation_ids")
        a._require(isinstance(event["evidence_refs"], list) and all(isinstance(anchor, dict)
                   and isinstance(anchor.get("chunk_id"), str) and type(anchor.get("start")) is int
                   and type(anchor.get("end")) is int for anchor in event["evidence_refs"]),
                   "hold evidence requires literal chunk IDs and integer character offsets")
        a._require(review._anchors(context, event["evidence_refs"], "hold evidence") <= chapters,
                   "hold evidence is outside the declared chapters")
        a._require(a._is_sha256(event["discovery_sha256"]), "invalid discovery hash")
        a._require(isinstance(event["classification"], str) and event["classification"] in {"pending", "confirmed"}, "unknown hold classification")
    else:
        a._require(a._is_sha256(event["release_sha256"]), "invalid resolution release hash")
        a._require(isinstance(event["resolution"], str) and event["resolution"] in {"reviewed", "false_positive"}, "unknown resolution")
        locks = event["issue_locks"]
        a._require(isinstance(locks, list) and bool(locks) and all(isinstance(lock, dict) for lock in locks), "resolution needs issue locks")
        _ids([lock.get("issue_id") for lock in locks], "resolution issue IDs", nonempty=True)
        a._require(all(set(lock) == {"issue_id", "record_sha256"} and a._is_sha256(lock["record_sha256"]) for lock in locks), "invalid resolution issue lock")
        stages = event["stage_locks"]
        a._require(isinstance(stages, dict) and bool(stages), "resolution needs stage locks")
        for batch, selection in stages.items():
            a._require(batch in context.batch_by_id and isinstance(selection, dict) and set(selection) == set(a.STAGES)
                       and all(a._is_sha256(digest) for digest in selection.values()), "invalid resolution stage locks")
    if sealed:
        a._require(event["schema_version"] == 1 and event["record_type"] == "alignment_review_hold_event", "unsupported hold event contract")
        a._require(type(event["sequence"]) is int and event["sequence"] > 0, "invalid hold sequence")
        a._require(event["previous_event_sha256"] is None or a._is_sha256(event["previous_event_sha256"]), "invalid previous hold hash")
        a._require(all(event[key] == value for key, value in _pins(context).items()), "hold source/structure pins changed")
        a._require(event["record_sha256"] == a.record_sha256(event), "hold event hash mismatch")
        review.timestamp(event["created_utc"])
        if kind == "resolve":
            a._require(a._is_sha256(event["opened_record_sha256"]), "invalid held discovery binding")


def _validated(context, view, seen=None):
    """Reconstruct views from selected bytes; mutable derived fields are not proof."""
    a._require(isinstance(view, r.ReleaseView), "resolution requires a resolved ReleaseView")
    r.checked_manifest(view.manifest)
    a._require(isinstance(view.files, dict) and set(view.files) == set(view.manifest["files"])
               and all(isinstance(payload, bytes) and r.sha(payload) == view.manifest["files"][name]
                       for name, payload in view.files.items()), "hold release selected file bytes/hash mismatch")
    seen = set() if seen is None else seen
    a._require(id(view) not in seen, "hold release ancestry cycle")
    seen.add(id(view))
    if view.parent is None:
        return r._baseline_snapshot(context, view.manifest, view.files)
    parent = _validated(context, view.parent, seen)
    return r.resolve(context, view.manifest, r.object_map(view.files), parent=parent)


def _raw_ranges(context, anchor):
    """Rebuild frozen chunk slices from source, not unhashed slice metadata."""
    from tools.build_chunks import normalize_units, split_paragraphs, MAX_CHARS, TARGET_CHARS

    chapter = anchor["chunk_id"].split("_", 1)[0]
    row = next(row for row in context.chunks_by_chapter[chapter] if row["chunk_id"] == anchor["chunk_id"])
    chapter_row = context.chapter_by_id[chapter]
    first, last = chapter_row["source_line_body_start"], chapter_row["source_line_end"]
    units = normalize_units(split_paragraphs(context.source_lines[first - 1:last], first))
    groups, current, size = [], [], 0
    for unit in units:
        if current and size + len(unit["text"]) > MAX_CHARS:
            groups.append(current)
            current, size = [], 0
        current.append(unit)
        size += len(unit["text"])
        if size >= TARGET_CHARS:
            groups.append(current)
            current, size = [], 0
    if current:
        groups.append(current)
    a._require(0 < row["chunk_index"] <= len(groups), "hold chunk reconstruction failed")
    slices = groups[row["chunk_index"] - 1]
    a._require(slices[0]["source_line_start"] == row["source_line_start"] and
               slices[-1]["source_line_end"] == row["source_line_end"] and
               len(slices) == row["source_slice_count"], "hold source slice inventory differs from frozen index")
    pieces = [part["text"] for part in slices]
    joined = "\n\n".join(pieces)
    a._require(joined.strip() == context.chunk_text[anchor["chunk_id"]], "hold source slices do not reproduce frozen chunk")
    trim = len(joined) - len(joined.lstrip())
    start, end = anchor["start"] + trim, anchor["end"] + trim
    result, offset = [], 0
    for part, piece in zip(slices, pieces):
        left, right = max(start, offset), min(end, offset + len(piece))
        if left < right:
            result.append((chapter, part["source_line_start"], part["char_start"] + left - offset,
                           part["char_start"] + right - offset))
        offset += len(piece) + 2
    a._require(bool(result), "hold evidence contains no source characters")
    return result


def _covered_span(span, issue_ranges):
    """Require the same-chapter/line interval union to cover every held character."""
    chapter, line, start, end = span
    cursor = start
    for left, right in sorted((left, right) for ch, ln, left, right in issue_ranges if ch == chapter and ln == line):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def _resolution(context, opened, event, view):
    checked = _validated(context, view)
    a._require(event["release_sha256"] == checked.digest, "resolution release selection mismatch")
    actual = review.validate_review_gate(context, checked)
    a._require(event["reviewer_task_id"] != opened["reporter_task_id"], "hold reporter cannot resolve their own finding")
    ids = {lock["issue_id"] for lock in event["issue_locks"]}
    a._require(ids <= set(actual["affected_issue_ids"]), "resolution issues were not covered by this actual review")
    issues, chapters, raw = [], set(), []
    for lock in event["issue_locks"]:
        issue = checked.issues.get(lock["issue_id"])
        a._require(issue is not None and issue["record_sha256"] == lock["record_sha256"], "resolution issue version not selected")
        a._require(issue.get("_preflight_state") == "completed" and all(isinstance(issue.get(role), dict) for role in ("producer", "counter_review", "adjudication")), "resolution requires a complete three-role text adjudication")
        actors = [issue[role]["task_id"] for role in ("producer", "counter_review", "adjudication")]
        a._require(len(set(actors)) == 3 and issue["adjudication"]["task_id"] == event["reviewer_task_id"], "resolution reviewer must be the actual independent issue adjudicator")
        if event["resolution"] == "false_positive":
            a._require(issue.get("_preflight_disposition") == "no_repair", "false_positive requires formal no_repair")
        issues.append(issue)
        chapters.update([issue["chapter_id"], *issue.get("related_chapter_ids", [])])
        for anchor in issue["source_anchors"]:
            raw.append((anchor.get("chapter_id", issue["chapter_id"]), anchor["source_line"], *anchor["line_char_range"]))
    a._require(set(opened["chapter_ids"]) <= chapters, "resolution omits held chapters")
    for anchor in opened["evidence_refs"]:
        ranges = _raw_ranges(context, anchor)
        a._require(all(_covered_span(span, raw) for span in ranges),
                   "resolution issues do not address the held source evidence")
    a._require(set(opened["observation_ids"]) <= set(actual["affected_observation_ids"]), "resolution omits held observations")
    # Bind the complete actual candidate impact, including consumed remote scope.
    expected = {batch: {stage: checked.stages[stage][batch]["record_sha256"] for stage in a.STAGES}
                for batch in actual["batches"]}
    a._require(event["stage_locks"] == expected, "resolution stage locks omit or invent actual dependency closure")
    for chapter in opened["chapter_ids"]:
        a._require(context.chapter_to_batch[chapter] in expected, "held chapter lacks a new closed stage chain")
    return checked


class HoldStore:
    def __init__(self, root: Path):
        self.alignment_root = Path(root).absolute()
        self.root = self._path()

    def _path(self, relative=""):
        """Reject reparse components before resolving; check every read/write target."""
        parts = ("review_holds", *Path(relative).parts)
        path = self.alignment_root
        for component in (None, *parts):
            if component is not None:
                path = path / component
            try:
                attributes = getattr(path.lstat(), "st_file_attributes", 0)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise a.AlignmentError("cannot inspect hold path: " + str(path)) from exc
            a._require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)()
                       and not attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                       "hold path cannot contain a junction, symlink or reparse point: " + str(path))
        return a._repo_path(self.alignment_root, "/".join(parts))

    def _head(self, context, events, inventory):
        return a.with_record_hash({"schema_version": 1, "record_type": "alignment_review_hold_head", **_pins(context),
                                   "sequence": len(events), "last_event_sha256": events[-1]["record_sha256"] if events else None,
                                   "event_files": inventory})

    def _scan(self, context, *, recovering=False):
        root = self._path()
        if not root.exists():
            return [], {}, None
        a._require(root.is_dir(), "invalid hold store directory")
        a._require({p.name for p in root.iterdir()} <= {"head.json", "events"}, "unexpected hold store member")
        directory = self._path("events")
        a._require(directory.is_dir() and not directory.is_symlink(), "missing or invalid hold events directory")
        events, inventory, opened, resolutions = [], {}, {}, set()
        for member in sorted(directory.iterdir()):
            path = self._path("events/" + member.name)
            match = EVENT_NAME.fullmatch(path.name)
            a._require(match is not None and path.is_file() and not path.is_symlink(), "orphan or unexpected hold event member")
            event, payload = _json(path)
            _shape(context, event, sealed=True)
            a._require(int(match[1]) == event["sequence"] == len(events) + 1 and match[2] == event["record_sha256"], "hold event sequence/name mismatch")
            a._require(event["previous_event_sha256"] == (events[-1]["record_sha256"] if events else None), "broken hold previous hash chain")
            key = event["hold_id"]
            if event["event_type"] == "open":
                a._require(key not in opened, "duplicate hold open identity")
                opened[key] = event
            else:
                a._require(key in opened and event["opened_record_sha256"] == opened[key]["record_sha256"], "resolution lacks exact preceding open")
                a._require(event["reviewer_task_id"] != opened[key]["reporter_task_id"], "hold reporter cannot resolve their own finding")
                identity = key, event["release_sha256"]
                a._require(identity not in resolutions, "duplicate hold/release resolution")
                resolutions.add(identity)
            events.append(event)
            inventory[path.name] = r.sha(payload)
        head_path = self._path("head.json")
        if not head_path.exists():
            a._require(recovering and bool(events), "hold head missing; explicit recovery required")
            return events, inventory, None
        head, head_bytes = _json(head_path)
        a._require(head.get("record_sha256") == a.record_sha256(head), "hold head hash mismatch")
        count = head.get("sequence")
        a._require(type(count) is int and 0 <= count <= len(events), "hold head references missing events")
        prefix = {name: digest for i, (name, digest) in enumerate(inventory.items()) if i < count}
        a._require(head == self._head(context, events[:count], prefix), "hold head membership or pins mismatch")
        a._require(recovering or count == len(events), "pending/orphan hold event beyond head; explicit recovery required")
        a._require({path.name for path in self._path("events").iterdir()} == set(inventory)
                   and self._path("head.json").read_bytes() == head_bytes,
                   "hold inventory changed during read; retry")
        return events, inventory, head_bytes

    def events(self, context) -> list[dict]:
        return self._scan(context)[0]

    def fingerprint(self, context) -> str:
        _, inventory, head = self._scan(context)
        return r.sha(r.json_bytes({"events": inventory, "head.json": r.sha(head) if head is not None else None}))

    def append(self, context, event, view=None) -> dict:
        authored = copy.deepcopy(event)
        _shape(context, authored)
        with transaction.writer_lock(self.alignment_root):
            events, inventory, _ = self._scan(context)
            opened = {row["hold_id"]: row for row in events if row["event_type"] == "open"}
            key = authored["hold_id"]
            if authored["event_type"] == "open":
                a._require(key not in opened, "duplicate hold open identity")
            else:
                a._require(key in opened, "resolution has no preceding hold")
                a._require(not any(row["event_type"] == "resolve" and row["hold_id"] == key and row["release_sha256"] == authored["release_sha256"] for row in events), "duplicate hold/release resolution")
                _resolution(context, opened[key], authored, view)
                authored["opened_record_sha256"] = opened[key]["record_sha256"]
            sealed = a.with_record_hash({**authored, "schema_version": 1, "record_type": "alignment_review_hold_event", **_pins(context),
                "sequence": len(events) + 1, "previous_event_sha256": events[-1]["record_sha256"] if events else None,
                "created_utc": datetime.now(timezone.utc).isoformat()})
            _shape(context, sealed, sealed=True)
            name = f"{sealed['sequence']:012d}-{sealed['record_sha256']}.json"
            payload = r.json_bytes(sealed)
            r.Store._put(self._path("events/" + name), payload)
            inventory[name] = r.sha(payload)
            transaction.atomic_write(self._path("head.json"), r.json_bytes(self._head(context, [*events, sealed], inventory)))
            self._scan(context)
            return sealed

    def recover(self, context) -> list[dict]:
        """Only advance/reconstruct a head over a unique valid chain; no judgment."""
        with transaction.writer_lock(self.alignment_root):
            events, inventory, _ = self._scan(context, recovering=True)
            if not self._path().exists():
                return []
            transaction.atomic_write(self._path("head.json"), r.json_bytes(self._head(context, events, inventory)))
            return self.events(context)

    def active(self, context, view) -> dict[str, dict]:
        events = self.events(context)
        opened = {row["hold_id"]: row for row in events if row["event_type"] == "open"}
        if view is None or not any(row["event_type"] == "resolve" for row in events):
            return opened
        selected = _validated(context, view)
        ancestors = {}
        ancestor = selected
        while ancestor is not None:
            ancestors[ancestor.digest] = ancestor
            ancestor = ancestor.parent
        for event in events:
            if event["event_type"] != "resolve":
                continue
            issues_match = all(selected.issues.get(lock["issue_id"], {}).get("record_sha256") == lock["record_sha256"] for lock in event["issue_locks"])
            stages_match = all(selected.stages[stage].get(batch, {}).get("record_sha256") == digest
                               for batch, stages in event["stage_locks"].items() for stage, digest in stages.items())
            bound = ancestors.get(event["release_sha256"])
            if issues_match and stages_match and bound is not None:
                original = next(row for row in events if row["event_type"] == "open" and row["hold_id"] == event["hold_id"])
                try:
                    _resolution(context, original, event, bound)
                except a.AlignmentError:
                    # A correctly hashed handwritten event is not proof of a
                    # valid release, actual judgment or independent reviewer.
                    continue
                opened.pop(event["hold_id"], None)
        return opened
