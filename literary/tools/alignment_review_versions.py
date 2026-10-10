"""Explicit feedback provenance for receipt protocol 2, never an approval builder.

Early discovery/review documents are immutable evidence, not invented stages.
The original first exposure, current-version review, and later assembly check
have separate clocks. All literary decisions still require authored receipts.
"""
from __future__ import annotations

import copy
import json
import re

from literary.tools import alignment as a, alignment_release as r


DOCUMENT_PATTERN = re.compile(r"review_documents/(FA-V\d{2}-\d{2})/[a-z0-9][a-z0-9.-]{0,95}\.json\Z")
DOCUMENT_FIELDS = {
    "schema_version", "record_type", "document_kind", "batch_id", "stage", "task_id",
    "finished_utc", "recorded_utc", "provenance_disclosure", "reading_chapter_locks",
    "body_utf8", "body_sha256", "body_size_bytes", "consumed_documents", "record_sha256",
}
LOCK_FIELDS = {"path", "file_sha256", "record_sha256"}
ACCESS_FIELDS = LOCK_FIELDS | {"accessed_utc", "review_finished_utc"}


def protocol(plan):
    version = plan.get("receipt_protocol_version", 1)
    a._require(type(version) is int and version in (1, 2), "unsupported receipt protocol version")
    return version


def _time(value):
    from literary.tools.alignment_review import timestamp
    return timestamp(value)


def _prose(value, label):
    from literary.tools.alignment_review import _prose as check
    check(value, label)


def _document_lock(documents, files, lock, *, access=False):
    fields = ACCESS_FIELDS if access else LOCK_FIELDS
    a._require(isinstance(lock, dict) and set(lock) == fields, "invalid review document lock fields")
    path = lock["path"]
    a._require(isinstance(path, str) and path in documents, "review document is not selected")
    doc = documents[path]
    a._require(lock["file_sha256"] == r.sha(files[path]) and lock["record_sha256"] == doc["record_sha256"],
               "review document version mismatch")
    if access:
        a._require(_time(doc["finished_utc"]) <= _time(lock["accessed_utc"]) <= _time(lock["review_finished_utc"]),
                   "review document accessed before it existed or reversed review")
    return doc


def validate_documents(context, files, parent=None):
    """E1 validates exact immutable document bytes even before E2 is attached."""
    documents = {}
    for path, payload in files.items():
        if not path.startswith("review_documents/"):
            continue
        match = DOCUMENT_PATTERN.fullmatch(path)
        a._require(match is not None, "unapproved review document path")
        try:
            doc = json.loads(payload.decode("utf-8"), object_pairs_hook=a._unique_object)
        except (ValueError, UnicodeError) as exc:
            raise a.AlignmentError("invalid review document JSON") from exc
        a._require(isinstance(doc, dict) and set(doc) == DOCUMENT_FIELDS, "invalid review document fields")
        a._require(doc["schema_version"] == 1 and doc["record_type"] == "authored_review_document",
                   "review document is not a discovery/review artifact; cannot substitute a stage")
        a._require(doc["record_sha256"] == a.record_sha256(doc), "review document record hash mismatch")
        a._require(doc["batch_id"] == match.group(1) and doc["batch_id"] in context.batch_by_id,
                   "review document batch mismatch")
        a._require(doc["stage"] in a.STAGES, "invalid review document role")
        _prose(doc["task_id"], "review document author")
        _prose(doc["provenance_disclosure"], "review document provenance")
        a._require(doc["document_kind"] in {"discovery", "blind_findings", "review", "response"},
                   "invalid review document kind")
        a._require(doc["document_kind"] != "blind_findings" or doc["stage"] == "counter_review",
                   "blind findings must belong to the actual counter")
        a._require(_time(doc["finished_utc"]) <= _time(doc["recorded_utc"]), "document completed after it was recorded")
        a._require(isinstance(doc["body_utf8"], str) and bool(doc["body_utf8"].strip()), "empty review document body")
        body = doc["body_utf8"].encode("utf-8")
        a._require(type(doc["body_size_bytes"]) is int and len(body) == doc["body_size_bytes"]
                   and r.sha(body) == doc["body_sha256"], "review document original bytes mismatch")
        locks = doc["reading_chapter_locks"]
        a._require(isinstance(locks, list) and bool(locks) and all(
            isinstance(lock, dict) and set(lock) == {"chapter_id", "reading_chapter_sha256"}
            and lock["chapter_id"] in context.chapter_by_id and a._is_sha256(lock["reading_chapter_sha256"])
            for lock in locks), "review document needs explicit input chapter version locks")
        a._require([lock["chapter_id"] for lock in locks] == sorted({lock["chapter_id"] for lock in locks}),
                   "review document chapter locks must be unique and ordered")
        a._require(isinstance(doc["consumed_documents"], list), "review document consumed inputs must be explicit")
        documents[path] = doc
    if parent:
        for path, payload in parent.files.items():
            if path.startswith("review_documents/"):
                a._require(files.get(path) == payload, "historical review document omitted or rewritten")
    edges = {}
    for path, doc in documents.items():
        consumed = []
        for lock in doc["consumed_documents"]:
            other = _document_lock(documents, files, lock, access=True)
            a._require(other["batch_id"] == doc["batch_id"], "cross-batch review document consumption")
            a._require(_time(lock["review_finished_utc"]) <= _time(doc["finished_utc"]),
                       "document consumes a later review")
            consumed.append(lock["path"])
        a._require(len(consumed) == len(set(consumed)), "duplicate consumed review document")
        edges[path] = consumed
    visited, active = set(), set()
    def visit(path):
        a._require(path not in active, "cycle in review document provenance")
        if path in visited:
            return
        active.add(path)
        for previous in edges[path]:
            visit(previous)
        active.remove(path)
        visited.add(path)
    for path in documents:
        visit(path)
    return documents


def validate_document_input_versions(documents, reading_hashes, parent):
    """Do not treat an arbitrary plausible hash as an attested reading input."""
    known = {key: {digest} for key, digest in reading_hashes.items()}
    while parent:
        for key, digest in parent.reading_hashes.items():
            known[key].add(digest)
        parent = parent.parent
    for doc in documents.values():
        a._require(all(lock["reading_chapter_sha256"] in known[lock["chapter_id"]]
                       for lock in doc["reading_chapter_locks"]), "review document input reading version is not validated")


def validate_versioned_receipt(context, view, batch, stage, scope, receipts, documents):
    """Current judgments bind current inputs; first exposure binds actual P0."""
    receipt = receipts[stage]
    a._require(receipt.get("receipt_protocol_version") == 2, "mixed receipt protocols in one review plan")
    modeled = set(scope["reading_chapter_ids"])
    for doc in documents.values():
        if doc["batch_id"] == batch:
            a._require({lock["chapter_id"] for lock in doc["reading_chapter_locks"]} <= modeled,
                       "review document consumes unmodeled context; declare it in the candidate stage")
    first_time, finish = _time(receipt["started_utc"]), _time(receipt["finished_utc"])
    upstream = a.STAGES[:a.STAGES.index(stage)]
    inputs = receipt.get("reviewed_inputs")
    a._require(isinstance(inputs, dict) and set(inputs) == set(upstream), "current review input set mismatch")
    for role in upstream:
        entry = inputs[role]
        a._require(isinstance(entry, dict) and set(entry) == {
            "stage_record_sha256", "receipt_sha256", "accessed_utc", "review_finished_utc"}, "current review input fields mismatch")
        a._require(entry["stage_record_sha256"] == view.stages[role][batch]["record_sha256"]
                   and entry["receipt_sha256"] == receipts[role]["record_sha256"], "current reviewed input version mismatch")
        a._require(first_time <= _time(entry["accessed_utc"]) <= _time(entry["review_finished_utc"]) <= finish
                   and _time(receipts[role]["finished_utc"]) <= _time(entry["accessed_utc"]),
                   "current version was not actually reviewed after the selected upstream existed")
    prior = receipt.get("prior_review_documents")
    a._require(isinstance(prior, list), "prior review documents must be explicit")
    paths = []
    for entry in prior:
        doc = _document_lock(documents, view.files, entry, access=True)
        a._require(doc["batch_id"] == batch and first_time <= _time(entry["accessed_utc"])
                   <= _time(entry["review_finished_utc"]) <= finish, "prior document review scope/time mismatch")
        paths.append(entry["path"])
    a._require(len(paths) == len(set(paths)), "duplicate prior review document")
    if stage != "counter_review":
        a._require("first_production_access" not in receipt and "blind_findings_document" not in receipt,
                   "only the counter can attest blind findings/first production exposure")
        return
    reading = receipt["reading"]
    if reading["kind"] == "same_bytes_reference":
        # Historical blind proof remains in the validated ancestor. It is not
        # relabeled as this supplement author's new first exposure.
        a._require("first_production_access" not in receipt and "blind_findings_document" not in receipt,
                   "same-byte supplement must not invent a new blind reading")
        return
    first = receipt.get("first_production_access")
    doc = _document_lock(documents, view.files, first, access=True)
    a._require(doc["stage"] == "production" and doc["task_id"] == receipts["production"]["task_id"]
               and doc["batch_id"] == batch, "first production document author/role/batch mismatch")
    a._require(doc["reading_chapter_locks"] == reading["reading_chapter_locks"],
               "first production document input version/scope differs from the blind reading")
    first_access = _time(first["accessed_utc"])
    a._require(first_access == _time(reading["production_accessed_utc"])
               and first_time <= first_access <= _time(first["review_finished_utc"]) <= finish,
               "first exposure must preserve the original actual access time")
    blind = _document_lock(documents, view.files, receipt.get("blind_findings_document"))
    a._require(blind["document_kind"] == "blind_findings" and blind["stage"] == stage
               and blind["task_id"] == receipt["task_id"] and blind["batch_id"] == batch,
               "blind findings document author/role/batch mismatch")
    a._require(not blind["consumed_documents"], "blind findings cannot consume prior literary opinion documents")
    a._require(_time(reading["finished_utc"]) <= _time(blind["finished_utc"]) <= first_access,
               "independent findings were not recorded before first production exposure")
    a._require(blind["reading_chapter_locks"] == reading["reading_chapter_locks"],
               "blind findings document input version/scope mismatch")
    # A frozen feedback document may record an earlier real P0 access. Later
    # final-version access cannot overwrite that already declared history.
    known_accesses = [first_access]
    foreign_opinion_accesses = [first_access]
    for history in documents.values():
        if history["batch_id"] == batch and history["task_id"] == receipt["task_id"]:
            for lock in history["consumed_documents"]:
                consumed = documents[lock["path"]]
                if consumed["stage"] == "production":
                    known_accesses.append(_time(lock["accessed_utc"]))
                if consumed["task_id"] != receipt["task_id"]:
                    foreign_opinion_accesses.append(_time(lock["accessed_utc"]))
    for entry in prior:
        consumed = documents[entry["path"]]
        if consumed["stage"] == "production":
            known_accesses.append(_time(entry["accessed_utc"]))
        if consumed["task_id"] != receipt["task_id"]:
            foreign_opinion_accesses.append(_time(entry["accessed_utc"]))
    a._require(first_access == min(known_accesses), "later exposure cannot replace an earlier documented first access")
    a._require(min(foreign_opinion_accesses) >= _time(blind["finished_utc"]),
               "counter saw another literary opinion before completing independent blind findings")
    a._require(first_access <= _time(inputs["production"]["accessed_utc"]), "current production access predates first exposure")


def assembly_subject(manifest):
    """Complete immutable selection, excluding only its hash and later ACKs."""
    subject = copy.deepcopy(manifest)
    subject.pop("record_sha256", None)
    subject["review_plan"].pop("assembly_acks", None)
    return r.sha(r.json_bytes(subject))


def validate_assembly(view, documents):
    plan = view.manifest["review_plan"]
    acknowledgments = plan.get("assembly_acks")
    a._require(isinstance(acknowledgments, dict) and set(acknowledgments) == set(plan["batches"]),
               "assembly acknowledgments must cover every affected batch")
    subject = assembly_subject(view.manifest)
    # Every ACK signs the whole selection, including the other batches and
    # every selected document. A local-only clock would attest future reviews.
    subject_times = [_time(receipt["finished_utc"]) for receipts in plan["batches"].values()
                     for receipt in receipts.values()]
    subject_times.extend(_time(doc["recorded_utc"]) for doc in documents.values())
    last_review = max(subject_times) if subject_times else None
    for batch, receipts in plan["batches"].items():
        group = acknowledgments[batch]
        a._require(isinstance(group, dict) and set(group) == set(a.STAGES), "assembly needs three actual author acknowledgments")
        for stage, ack in group.items():
            a._require(isinstance(ack, dict) and set(ack) == {
                "stage", "task_id", "selection_subject_sha256", "signed_utc", "acknowledgment", "record_sha256"},
                "invalid assembly acknowledgment fields")
            a._require(ack["record_sha256"] == a.record_sha256(ack) and ack["stage"] == stage
                       and ack["task_id"] == receipts[stage]["task_id"], "assembly acknowledgment author/hash mismatch")
            a._require(ack["selection_subject_sha256"] == subject, "assembly selection subject mismatch")
            a._require(last_review is not None and _time(ack["signed_utc"]) >= last_review,
                       "assembly signed before the actual final review/documents of the whole selection")
            _prose(ack["acknowledgment"], "assembly acknowledgment is not a new literary approval")
