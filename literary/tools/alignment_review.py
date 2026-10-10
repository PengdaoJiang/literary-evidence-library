"""Authored revision receipts: reading evidence is not inherited approval.

This gate verifies bindings, coverage and explicit attestations. It cannot prove
that a reader understood the text; independent semantic review remains required.
There is deliberately no metadata-only exemption or automatic approval builder.
"""
from __future__ import annotations

from datetime import datetime, timezone

from literary.tools import alignment as a, alignment_release as r, alignment_v2 as v2


def timestamp(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace(" UTC", "+00:00").replace("Z", "+00:00"))
        a._require(result.tzinfo is not None, "reading times require a timezone")
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as exc:
        raise a.AlignmentError("invalid reading timestamp") from exc


def reading_locks(view: r.ReleaseView, chapter_ids: list[str]) -> list[dict]:
    return [{"chapter_id": key, "reading_chapter_sha256": view.reading_hashes[key]} for key in chapter_ids]


def _prose(value, label):
    a._require(isinstance(value, str) and bool(value.strip()), label + ": authored prose must be a nonempty string")
    errors = []
    a._check_human_text(value, label, errors)
    a._require(not errors, "; ".join(errors))


def _known_finish(view, batch, stage):
    row = view.stages[stage].get(batch, {})
    digest = row.get("record_sha256")
    finishes = []
    old_finish = row.get("reading_process", {}).get("full_chunk_read_finished_utc")
    if old_finish:
        finishes.append(timestamp(old_finish))
    while view:
        receipt = view.manifest.get("review_plan", {}).get("batches", {}).get(batch, {}).get(stage, {})
        if receipt.get("stage_record_sha256") == digest and receipt.get("finished_utc"):
            finishes.append(timestamp(receipt["finished_utc"]))
        view = view.parent
    return max(finishes) if finishes else None


def _chapters(context, value, label):
    a._require(isinstance(value, list) and all(isinstance(key, str) and key in context.chapter_by_id for key in value),
               label + ": expected known chapter IDs")
    a._require(value == sorted(set(value)), label + ": chapter IDs must be unique and ordered")
    return value


def _anchors(context, value, label):
    a._require(isinstance(value, list) and bool(value), label + ": actual evidence anchors required")
    errors = []
    for anchor in value:
        a._require(isinstance(anchor, dict), label + ": anchor must be an object")
        a._validate_chunk_anchor(context, anchor, label, errors)
    a._require(not errors, "; ".join(errors))
    return {anchor["chunk_id"].split("_", 1)[0] for anchor in value}


def _original_reading(context, view, batch, stage):
    """Resolve only attested coverage, never infer blind reading from a label."""
    row = view.stages[stage].get(batch)
    a._require(row is not None, "no original stage reading to reference")
    receipt = view.manifest.get("review_plan", {}).get("batches", {}).get(batch, {}).get(stage)
    if receipt:
        reading = receipt["reading"]
        if reading["kind"] == "same_bytes_reference":
            a._require(view.parent is not None, "reading reference lacks ancestor")
            return _original_reading(context, view.parent, batch, stage)
        a._require(reading["kind"] == "full_read", "original receipt is not a full reading")
        return set(reading["read_chapter_ids"]), reading.get("blind_read_completed_before_production") is True
    targets = [ch["chapter_id"] for ch in row["chapters"]]
    for ch in row["chapters"]:
        a._require(ch.get("read_chunk_ids") == [item["chunk_id"] for item in context.chunks_by_chapter[ch["chapter_id"]]],
                   "original reading lacks ordered full target coverage")
    process = row.get("reading_process", {})
    boundaries = _chapters(context, process.get("boundary_read_chapter_ids", []), "original boundaries")
    # A missing explicit blind disclosure is not repaired by inventing one now.
    return set(targets + boundaries), process.get("blind_read_completed_before_production") is True


def _validate_reading(context, view, batch, stage, scope, receipt):
    reading = receipt.get("reading")
    a._require(isinstance(reading, dict), "revision requires an actual reading receipt")
    kind = reading.get("kind")
    start, finish = timestamp(receipt.get("started_utc")), timestamp(receipt.get("finished_utc"))
    a._require(start <= finish, "reversed review interval")
    _prose(receipt.get("prior_exposure_disclosure"), "prior exposure disclosure")
    chapters = _chapters(context, reading.get("read_chapter_ids"), "reading scope")
    if stage == "adjudication":
        a._require(kind == "targeted_adjudication", "adjudication needs its own actual targeted review")
        evidence_chapters = _anchors(context, reading.get("evidence_refs"), "adjudication reading")
        lock_ids = sorted(set(chapters) | evidence_chapters)
    else:
        a._require(set(scope["reading_chapter_ids"]) <= set(chapters), "reading omits affected target/context")
        lock_ids = chapters
        chunks = [chunk["chunk_id"] for chapter in chapters for chunk in context.chunks_by_chapter[chapter]]
        a._require(reading.get("ordered_read_chunk_ids") == chunks, "reading must cover full ordered chapter chunks")
        if kind == "same_bytes_reference":
            a._require(scope["mode"] == "supplement" and view.parent is not None, "changed text/new reading cannot reuse old reading")
            original = view.parent.stages[stage].get(batch)
            a._require(original is not None, "no parent reading to reuse")
            a._require(reading.get("source_release_sha256") == view.parent.digest and
                       reading.get("source_stage_record_sha256") == original["record_sha256"] and
                       reading.get("source_task_id") == original["task_id"], "old reading provenance mismatch")
            covered, blind = _original_reading(context, view.parent, batch, stage)
            known_finish = _known_finish(view.parent, batch, stage)
            a._require(known_finish is None or finish >= known_finish, "supplement finished before its referenced historical reading/review")
            a._require(set(chapters) <= covered, "old receipt did not attest the claimed full context")
            a._require(all(view.parent.reading_hashes[key] == view.reading_hashes[key] for key in chapters),
                       "same-byte receipt refers to changed reading text")
            if stage == "counter_review":
                a._require(blind, "original counter has no explicit blind-reading disclosure; reread required")
        else:
            a._require(kind == "full_read", "new production/counter needs full_read or validated same-byte reference")
            read_start, read_finish = timestamp(reading.get("started_utc")), timestamp(reading.get("finished_utc"))
            a._require(start <= read_start <= read_finish <= finish, "full reading is outside the actual review interval")
            if stage == "counter_review":
                access = timestamp(reading.get("production_accessed_utc"))
                a._require(reading.get("blind_read_completed_before_production") is True and read_finish <= access <= finish,
                           "counter must complete blind full reading before seeing production")
            if view.parent:
                old = view.parent.stages[stage].get(batch, {}).get("reading_process", {})
                if old.get("full_chunk_read_started_utc") and old.get("full_chunk_read_finished_utc"):
                    a._require((read_start, read_finish) !=
                               (timestamp(old["full_chunk_read_started_utc"]), timestamp(old["full_chunk_read_finished_utc"])),
                               "old read times cannot masquerade as a fresh full reading")
                ancestor = view.parent
                while ancestor:
                    prior = ancestor.manifest.get("review_plan", {}).get("batches", {}).get(batch, {}).get(stage, {}).get("reading", {})
                    if prior.get("kind") == "full_read":
                        a._require((read_start, read_finish) != (timestamp(prior.get("started_utc")), timestamp(prior.get("finished_utc"))),
                                   "ancestral full-read interval copied as new reading")
                    ancestor = ancestor.parent
    a._require(reading.get("reading_chapter_locks") == reading_locks(view, lock_ids), "reading context version locks mismatch")


def _item_reviews(context, view, receipt, kind, ids, protocol_version=1):
    field, id_field = ("issue_reviews", "issue_id") if kind == "issues" else ("observation_reviews", "observation_id")
    reviews = receipt.get(field)
    a._require(isinstance(reviews, list) and all(isinstance(item, dict) for item in reviews), field + " must be an explicit list")
    a._require([item.get(id_field) for item in reviews] == sorted(ids), field + " must exhaustively cover affected IDs in order")
    current = getattr(view, kind)
    previous = getattr(view.parent, kind) if view.parent else {}
    for item in reviews:
        key = item[id_field]
        subject = current.get(key) or previous.get(key)
        a._require(subject and item.get("record_sha256") == subject["record_sha256"], field + ": wrong subject version")
        _prose(item.get("rationale"), field + ": actual new judgment rationale")
        _prose(item.get("decision"), field + ": actual new judgment decision")
        if kind == "issues":
            expected_decision = subject.get("_preflight_disposition") if key in current else "superseded"
        elif receipt["stage"] == "production":
            expected_decision = "proposed"
        else:
            owner = view.stages[receipt["stage"]].get(context.chapter_to_batch[subject["chapter_id"]], {})
            decisions = [entry for chapter in owner.get("chapters", []) for entry in chapter.get("observation_dispositions", [])
                         if entry.get("observation_id") == key]
            expected_decision = (decisions[0].get("recommendation" if receipt["stage"] == "counter_review" else "handoff_disposition")
                                 if decisions else "reviewed_dependency")
        a._require(item["decision"] == expected_decision, field + ": receipt decision differs from selected judgment")
        if kind == "observations":
            if protocol_version == 1 or receipt["stage"] == "adjudication":
                a._require(item.get("selected_disposition") == v2.effective_observation_dispositions(view.stages, view.observations)[key],
                           "observation receipt selected disposition mismatch")
            else:
                a._require("selected_disposition" not in item,
                           "early role opinions must not be rewritten as final adjudication; use the later assembly acknowledgment")
        evidence_chapters = _anchors(context, item.get("evidence_refs"), field + ":" + key)
        locked_chapters = {lock["chapter_id"] for lock in receipt["reading"]["reading_chapter_locks"]}
        a._require(evidence_chapters <= locked_chapters, "item review consumes unlocked remote evidence")
        # Catch literal transfer of prior decisions, not merely changed task IDs.
        if view.parent:
            for old_stage in view.parent.stages.values():
                for old in old_stage.values():
                    for chapter in old["chapters"]:
                        for disposition in chapter.get("observation_dispositions", []):
                            if kind == "observations" and disposition.get("observation_id") == key:
                                a._require(item["rationale"] != disposition.get("rationale"), "affected approval was copied from history")


def validate_review_gate(context: a.Context, view: r.ReleaseView) -> dict:
    """Mandatory activation gate; E1 candidate storage does not call this."""
    from literary.tools.alignment_impact import impact
    a._require(view.parent is not None, "baseline registration is not a new literary release")
    actual = impact(context, view.parent, view)
    plan = view.manifest.get("review_plan")
    a._require(isinstance(plan, dict) and plan.get("impact_sha256") == r.sha(r.json_bytes(actual)), "review impact fingerprint mismatch")
    from literary.tools import alignment_review_versions as versions
    protocol_version = versions.protocol(plan)
    documents = versions.validate_documents(context, view.files, view.parent)
    if protocol_version == 1:
        a._require(not documents and "assembly_acks" not in plan,
                   "versioned review documents/assembly require explicit receipt protocol 2")
    batches = plan.get("batches")
    a._require(isinstance(batches, dict) and set(batches) == set(actual["batches"]), "review plan omits or invents affected batches")
    for key in actual["changed_issue_ids"]:
        issue = view.issues.get(key)
        if issue:
            a._require(issue.get("_preflight_state") == "completed" and
                       all(isinstance(issue.get(role), dict) for role in ("producer", "counter_review", "adjudication")),
                       "changed issue needs actual independent three-stage adjudication")
    for batch, scope in actual["batches"].items():
        receipts = batches[batch]
        a._require(isinstance(receipts, dict) and set(receipts) == set(a.STAGES), "affected batch needs all three independent receipts")
        for stage in a.STAGES:
            row = view.stages[stage].get(batch)
            old = view.parent.stages[stage].get(batch)
            a._require(row is not None and row.get("schema_version") == 2 and
                       (old is None or row["record_sha256"] != old["record_sha256"]), "affected stage requires a new actual review version")
            receipt = receipts[stage]
            a._require(isinstance(receipt, dict) and receipt.get("record_sha256") == a.record_sha256(receipt), "review receipt hash mismatch")
            if protocol_version == 1:
                a._require(receipt.get("receipt_protocol_version", 1) == 1 and not any(key in receipt for key in (
                    "first_production_access", "blind_findings_document", "reviewed_inputs", "prior_review_documents")),
                    "versioned receipt fields require explicit receipt protocol 2")
            a._require(receipt.get("stage") == stage and receipt.get("task_id") == row["task_id"] and
                       receipt.get("stage_record_sha256") == row["record_sha256"] and
                       receipt.get("parent_stage_record_sha256") == (old["record_sha256"] if old else None), "review receipt stage/author/parent binding mismatch")
            expected_upstream = {s: receipts[s]["record_sha256"] for s in a.STAGES[:a.STAGES.index(stage)]}
            a._require(receipt.get("upstream_receipt_locks") == expected_upstream, "independent review upstream receipt mismatch")
            a._require(all(timestamp(receipt.get("finished_utc")) >= timestamp(receipts[s].get("finished_utc"))
                           for s in a.STAGES[:a.STAGES.index(stage)]), "review finished before its locked upstream receipt")
            if protocol_version == 1 and stage == "counter_review" and receipt.get("reading", {}).get("kind") == "full_read":
                a._require(timestamp(receipt["reading"].get("production_accessed_utc")) >= timestamp(receipts["production"].get("finished_utc")),
                           "counter accessed production before the selected production receipt existed")
            _validate_reading(context, view, batch, stage, scope, receipt)
            if protocol_version == 2:
                versions.validate_versioned_receipt(context, view, batch, stage, scope, receipts, documents)
            _item_reviews(context, view, receipt, "issues", scope["affected_issue_ids"], protocol_version)
            _item_reviews(context, view, receipt, "observations", scope["affected_observation_ids"], protocol_version)
            reviews = receipt.get("chapter_reviews")
            expected_ids = [lock["chapter_id"] for lock in context.batch_by_id[batch]["chapter_locks"]]
            a._require(isinstance(reviews, list) and all(isinstance(item, dict) for item in reviews) and
                       [item.get("chapter_id") for item in reviews] == expected_ids, "actual per-chapter revision review required")
            for item in reviews:
                _prose(item.get("rationale"), "actual per-chapter revision review")
            if stage == "adjudication":
                a._require(all(review.get("final_status") == chapter["final_status"]
                               for review, chapter in zip(reviews, row["chapters"])), "chapter receipt differs from selected terminal judgment")
        a._require(all(ch["final_status"] in a.TERMINAL_STATUSES for ch in view.stages["adjudication"][batch]["chapters"]),
                   "affected batch is not terminally closed")
    errors = []
    v2.validate_observation_references(view.stages, view.observations, context, errors, final=True,
                                      historical_owners=r._ancestral_owners(view.parent))
    a._require(not errors, "release observation closure: " + "; ".join(errors))
    if protocol_version == 2:
        versions.validate_assembly(view, documents)
    return actual
