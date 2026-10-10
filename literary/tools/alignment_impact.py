"""Read-only E2 impact analysis over two resolved release selections.

This is a dependency report, not a review, approval, hold, or activation gate.
Callers supply validated ReleaseViews (including complete reading_hashes). Only
structured ID fields are references: prose is never mined for guessed links.
Fields named <kind>_id(s), or ending in _<kind>_id(s), are recognized recursively;
chapter/chunk references without a layer declaration conservatively consume the
critical reading context. Explicit raw/diplomatic/source/original anchors remain
semantic dependencies but do not assert consumption of changed critical bytes.
Related chapter IDs alone likewise describe semantic scope, not a reading claim.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from literary.tools import alignment as a
from literary.tools.alignment_release import ReleaseView


KINDS = ("chapter", "chunk", "issue", "patch", "observation")
RAW_LAYERS = {"raw", "diplomatic", "source", "original"}
# Only provenance/coverage locks are mechanical. Narrative, rationale, evidence,
# identity, issue routing and terminal status are deliberately NOT exempt.
CHAPTER_LOCK_FIELDS = {
    "chapter_id", "record_sha256", "raw_chapter_sha256", "reading_chapter_sha256", "chunk_locks",
    "neighbor_locks", "applied_patch_ids", "observation_locks", "read_chunk_ids",
    "observation_dispositions",
}
Node = tuple[str, str]


@dataclass
class _Refs:
    chapters: set[str] = field(default_factory=set)
    critical: set[str] = field(default_factory=set)
    nodes: set[Node] = field(default_factory=set)

    def merge(self, other: _Refs) -> None:
        self.chapters.update(other.chapters)
        self.critical.update(other.critical)
        self.nodes.update(other.nodes)


def _references(value: Any, chunks: dict[str, str], known: dict[str, set[str]]) -> _Refs:
    """Walk explicit references, including nested evidence and old supersedes."""
    result = _Refs()

    def walk(item: Any, critical: bool = True) -> None:
        if isinstance(item, dict):
            layer = item.get("reading_layer", item.get("text_layer", item.get("layer")))
            critical = critical and layer not in RAW_LAYERS
            for key, content in item.items():
                if key == "proposed_observation_id":
                    # An embedded missed-finding proposal can name a candidate
                    # that was never admitted as an observation. This is a
                    # declaration, not a dangling reference or approval. Its
                    # other evidence fields are still walked independently.
                    a._require(isinstance(content, str) and bool(content.strip()), "invalid proposed observation label")
                    if content not in known["observation"]:
                        continue
                kind = next((kind for kind in KINDS
                             if key in (kind + "_id", kind + "_ids")
                             or key.endswith(("_" + kind + "_id", "_" + kind + "_ids"))), None)
                if kind is not None:
                    identifiers = content if isinstance(content, list) else [content]
                    for identifier in identifiers:
                        # Null optional references carry no dependency.
                        if identifier is None:
                            continue
                        a._require(isinstance(identifier, str) and identifier in known[kind],
                                   f"unknown structured {kind} reference: {identifier!r}")
                        if kind in ("chapter", "chunk"):
                            chapter = chunks[identifier] if kind == "chunk" else identifier
                            result.chapters.add(chapter)
                            if critical and not key.startswith("related_"):
                                result.critical.add(chapter)
                        else:
                            result.nodes.add((kind, identifier))
                else:
                    walk(content, critical)
        elif isinstance(item, list):
            for member in item:
                walk(member, critical)

    walk(value)
    return result


def _changed(old: dict, new: dict) -> set[str]:
    # Resolved records carry their authoritative hash; derived loader fields do
    # not make an unchanged record a new literary version.
    return {key for key in old.keys() | new.keys()
            if key not in old or key not in new
            or old[key]["record_sha256"] != new[key]["record_sha256"]}


def _closure(seeds: set[Node], edges: dict[Node, set[Node]]) -> set[Node]:
    seen = set(seeds)
    pending = deque(sorted(seeds))
    while pending:
        for target in edges.get(pending.popleft(), ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return seen


def _bound_readings(view: ReleaseView, batch: str, stage: str, digest: str):
    """Find receipts for this exact selected stage, including reused ancestors."""
    seen = set()
    while view is not None:
        a._require(id(view) not in seen, "cycle in impact release ancestry")
        seen.add(id(view))
        selected = view.stages.get(stage, {}).get(batch)
        if selected is not None and selected["record_sha256"] == digest:
            receipt = view.manifest.get("review_plan", {}).get("batches", {}).get(batch, {}).get(stage)
            if receipt is not None:
                a._require(receipt.get("stage_record_sha256") == digest,
                           "impact reading receipt does not bind the selected stage")
                a._require(isinstance(receipt.get("reading"), dict), "impact receipt reading is missing")
                yield receipt["reading"]
        view = view.parent


def _stage_semantic_observations(parent: ReleaseView, child: ReleaseView) -> set[str]:
    """Changed selected judgments matter even if observation bodies are frozen."""
    by_chapter: dict[str, set[str]] = defaultdict(set)
    for view in (parent, child):
        for identifier, observation in view.observations.items():
            by_chapter[observation["chapter_id"]].add(identifier)
    changed = set()
    for stage in a.STAGES:
        old, new = parent.stages.get(stage, {}), child.stages.get(stage, {})
        for batch in old.keys() | new.keys():
            previous = {row["chapter_id"]: row for row in old.get(batch, {}).get("chapters", [])}
            current = {row["chapter_id"]: row for row in new.get(batch, {}).get("chapters", [])}
            for chapter in previous.keys() | current.keys():
                left, right = previous.get(chapter, {}), current.get(chapter, {})
                old_dispositions = {item["observation_id"]: {key: value for key, value in item.items()
                                                           if key != "record_sha256"}
                                    for item in left.get("observation_dispositions", [])}
                new_dispositions = {item["observation_id"]: {key: value for key, value in item.items()
                                                           if key != "record_sha256"}
                                    for item in right.get("observation_dispositions", [])}
                changed.update(identifier for identifier in old_dispositions.keys() | new_dispositions.keys()
                               if old_dispositions.get(identifier) != new_dispositions.get(identifier))
                old_content = {key: value for key, value in left.items() if key not in CHAPTER_LOCK_FIELDS}
                new_content = {key: value for key, value in right.items() if key not in CHAPTER_LOCK_FIELDS}
                if old_content != new_content:
                    changed.update(by_chapter[chapter])
    return changed


def _impact(context: a.Context, parent: ReleaseView, child: ReleaseView, *,
            pending_chapters: set[str] | None = None, pending_observations: set[str] | None = None) -> dict:
    """Return deterministic, local impact; never modify inputs or the filesystem.

    ``reading_chapter_ids`` is the full target + fixed-neighbor + actually
    consumed remote critical scope, in frozen chapter order (also reported for
    supplements; it is not a claim that anyone has reread it). A previously
    unselected batch is ``new_batch`` even when its text changed. Existing batches
    are ``full_reread`` only when that actual scope's reading hashes changed;
    other affected batches require ``supplement``. Stage selection changes are
    impacts even without changed content records. No metadata exemption exists.

    Dependency union includes parent/child selected records and reading receipts
    bound to those exact stages, looking through ancestors for a reused stage.
    New child receipts must not add dependencies absent from that modeled scope:
    first declare any extra reading in the candidate stage, then sign its receipt.
    This keeps the impact stable when receipts are attached. Validating actual
    independent reading, receipt hashes and provenance is the review gate's job.
    """
    order = {row["chapter_id"]: i for i, row in enumerate(context.chapters)}
    chapters = set(order)
    pending_chapters = set() if pending_chapters is None else pending_chapters
    pending_observations = set() if pending_observations is None else pending_observations
    a._require(pending_chapters <= chapters, "unknown pending chapter reference")
    for view in (parent, child):
        a._require(set(view.reading_hashes) == chapters,
                   "impact requires complete actual reading hashes for both releases")
    chunks = {row["chunk_id"]: chapter for chapter, rows in context.chunks_by_chapter.items()
              for row in rows}
    known = {"chapter": chapters, "chunk": set(chunks)}
    plural = {"issue": "issues", "patch": "patches", "observation": "observations"}
    for kind, name in plural.items():
        known[kind] = set(getattr(parent, name)) | set(getattr(child, name))
    a._require(pending_observations <= known["observation"], "unknown pending observation reference")
    changed_reading = {key for key in chapters if parent.reading_hashes[key] != child.reading_hashes[key]}
    changed = {kind: _changed(getattr(parent, name), getattr(child, name)) for kind, name in plural.items()}

    # Store both records' edges under their stable identity. An omitted old
    # dependency is not an escape hatch from review in this transition.
    refs: dict[Node, _Refs] = defaultdict(_Refs)
    targets: dict[Node, _Refs] = defaultdict(_Refs)
    supporting_inputs: dict[Node, _Refs] = defaultdict(_Refs)
    owners: dict[str, set[str]] = defaultdict(set)
    for view in (parent, child):
        for kind, name in plural.items():
            for identifier, row in getattr(view, name).items():
                found = _references(row, chunks, known)
                found.nodes.discard((kind, identifier))
                refs[kind, identifier].merge(found)
                # Only this validated, explicit issue field distinguishes a
                # comparison source from a disputed location. Old records and
                # related_chapter_ids keep their conservative target meaning.
                target_record = {key: value for key, value in row.items()
                                 if kind != "issue" or key != "supporting_source_anchors"}
                targets[kind, identifier].merge(_references(target_record, chunks, known))
                if kind == "issue":
                    supporting_inputs[kind, identifier].merge(_references(
                        row.get("supporting_source_anchors", []), chunks, known))
                if kind == "observation":
                    owners[identifier].add(context.chapter_to_batch[row["chapter_id"]])

    dependencies: dict[Node, set[Node]] = {node: set(found.nodes) for node, found in refs.items()}
    # A chapter's text issue affects claims grounded there even when an older
    # claim predates that issue ID. Do not connect all claims merely because
    # their batches share a neighbor, or turn batch membership into graph edges.
    for oid in known["observation"]:
        observation = ("observation", oid)
        for kind in ("issue", "patch"):
            for identifier in known[kind]:
                node = (kind, identifier)
                if refs[observation].chapters & targets[node].chapters:
                    dependencies[observation].add(node)
    # If a comparison source itself receives a real issue, reconsider the
    # judgment that consumed it. Do not reverse this edge: adding a comparison
    # to a new issue cannot invalidate independent claims in the source chapter.
    for consumer, supporting in supporting_inputs.items():
        for kind in ("issue", "patch"):
            for identifier in known[kind]:
                node = (kind, identifier)
                if node != consumer and supporting.chapters & targets[node].chapters:
                    dependencies[consumer].add(node)
    downstream: dict[Node, set[Node]] = defaultdict(set)
    for consumer, inputs in dependencies.items():
        for dependency in inputs:
            downstream[dependency].add(consumer)
            # Reconsider both ends of an observation's explicit upstream link,
            # including replacement lineage. Other references remain directed.
            if consumer[0] == dependency[0] == "observation":
                downstream[consumer].add(dependency)

    seeds = {(kind, identifier) for kind, ids in changed.items() for identifier in ids}
    seeds.update(("observation", identifier) for identifier in pending_observations)
    stage_semantic = _stage_semantic_observations(parent, child)
    a._require(stage_semantic <= known["observation"], "unknown observation in selected stage disposition")
    seeds.update(("observation", identifier) for identifier in stage_semantic)
    seeds.update(node for node, found in refs.items() if found.chapters & (changed_reading | pending_chapters))
    for pid in changed["patch"]:
        seeds.update(node for node in refs["patch", pid].nodes if node[0] == "issue")
    affected = _closure(seeds, downstream)
    affected_issues = {identifier for kind, identifier in affected if kind == "issue"}
    affected_observations = {identifier for kind, identifier in affected if kind == "observation"}

    batch_refs: dict[str, _Refs] = {}
    stage_changed: dict[str, list[str]] = defaultdict(list)
    old_selected: set[str] = set()
    new_selected: set[str] = set()
    for batch_id, plan in context.batch_by_id.items():
        fixed = set(plan["ordered_chapter_ids"])
        for key in ("previous_chapter_lock", "next_chapter_lock"):
            if plan.get(key):
                fixed.add(plan[key]["chapter_id"])
        a._require(fixed <= chapters, "unknown fixed batch reading chapter")
        batch_refs[batch_id] = _Refs(set(fixed), set(fixed))
    for stage in a.STAGES:
        old_rows, new_rows = parent.stages.get(stage, {}), child.stages.get(stage, {})
        old_selected.update(old_rows)
        new_selected.update(new_rows)
        old_choice = parent.manifest["stage_selection"].get(stage, {})
        new_choice = child.manifest["stage_selection"].get(stage, {})
        for batch_id in old_rows.keys() | new_rows.keys() | old_choice.keys() | new_choice.keys():
            a._require(batch_id in batch_refs, "unknown selected batch: " + batch_id)
            if (old_choice.get(batch_id) != new_choice.get(batch_id)
                    or batch_id not in old_rows or batch_id not in new_rows
                    or old_rows[batch_id]["record_sha256"] != new_rows[batch_id]["record_sha256"]):
                stage_changed[batch_id].append(stage)
            for row in (old_rows.get(batch_id), new_rows.get(batch_id)):
                if row is not None:
                    batch_refs[batch_id].merge(_references(row, chunks, known))
                    for reading in _bound_readings(parent, batch_id, stage, row["record_sha256"]):
                        batch_refs[batch_id].merge(_references(reading, chunks, known))
    # Legacy stages do not have observation locks. Owning chapter membership is
    # still real consumption; all discoveries in a selected view retain a route.
    for oid, batches in owners.items():
        for batch_id in batches:
            batch_refs[batch_id].nodes.add(("observation", oid))

    batches = {}
    for batch_id, scope in batch_refs.items():
        # The frozen plan is not evidence that an unread future batch consumed
        # its neighbors. Keep global discoveries, but require work only for a
        # real selected historical or candidate stage.
        if batch_id not in old_selected | new_selected:
            continue
        local = set(scope.chapters)
        for kind in ("issue", "patch"):
            scope.nodes.update((kind, identifier) for identifier in known[kind]
                               if targets[kind, identifier].chapters & local)
        consumed = _closure(scope.nodes, dependencies)
        critical = set(scope.critical)
        semantic = set(scope.chapters)
        for node in consumed:
            # Issue association and patch application do not attest remote
            # critical reading. Actual stage/receipt reading is already in
            # scope; observation evidence is also a consumed claim context.
            if node[0] == "observation":
                critical.update(refs[node].critical)
            semantic.update(refs[node].chapters)
        for stage, selected in child.stages.items():
            if batch_id not in selected:
                continue
            # Candidate receipts may sign the precomputed input set, not silently
            # expand it after the review plan has sealed its impact hash.
            for reading in _bound_readings(child, batch_id, stage, selected[batch_id]["record_sha256"]):
                receipt_refs = _references(reading, chunks, known)
                a._require(receipt_refs.chapters <= semantic and receipt_refs.critical <= critical
                           and receipt_refs.nodes <= consumed,
                           "new reading receipt adds unmodeled consumption; declare it in the candidate stage")
        batch_issues = {identifier for kind, identifier in consumed & affected if kind == "issue"}
        batch_observations = {identifier for kind, identifier in consumed & affected if kind == "observation"}
        changed_here = critical & changed_reading
        if not (changed_here or scope.chapters & pending_chapters or consumed & affected or batch_id in stage_changed):
            continue
        mode = ("new_batch" if batch_id not in old_selected else
                "full_reread" if changed_here else "supplement")
        batches[batch_id] = {
            "mode": mode,
            "reading_chapter_ids": sorted(critical, key=order.__getitem__),
            "changed_reading_chapter_ids": sorted(changed_here, key=order.__getitem__),
            "affected_issue_ids": sorted(batch_issues),
            "affected_observation_ids": sorted(batch_observations),
            "changed_stages": stage_changed.get(batch_id, []),
        }
    return {
        "changed_reading_chapter_ids": sorted(changed_reading, key=order.__getitem__),
        "changed_issue_ids": sorted(changed["issue"]),
        "changed_patch_ids": sorted(changed["patch"]),
        "changed_observation_ids": sorted(changed["observation"]),
        "changed_observation_judgment_ids": sorted(stage_semantic),
        "affected_issue_ids": sorted(affected_issues),
        "affected_observation_ids": sorted(affected_observations),
        "batches": {key: batches[key] for key in sorted(batches)},
    }


def impact(context: a.Context, parent: ReleaseView, child: ReleaseView) -> dict:
    """Read-only actual release change closure; see _impact for scope semantics."""
    return _impact(context, parent, child)


def hold_impact(context: a.Context, view: ReleaseView, chapter_ids, observation_ids) -> dict:
    """Report pending dependency scope without fabricating a text/version change.

    Only actually selected batches can be affected. Reading scope is a dependency
    inventory, not a reading receipt; no reread mode, approval, or confirmed text
    change is inferred from a hold. No hold event or release is written.
    """
    pending_chapters, pending_observations = set(chapter_ids), set(observation_ids)
    result = _impact(context, view, view, pending_chapters=pending_chapters,
                     pending_observations=pending_observations)
    return {
        "classification": "pending_impact",
        "chapter_ids": [row["chapter_id"] for row in context.chapters if row["chapter_id"] in pending_chapters],
        "observation_ids": sorted(pending_observations),
        "affected_issue_ids": result["affected_issue_ids"],
        "affected_observation_ids": result["affected_observation_ids"],
        "batches": {batch: {key: value for key, value in scope.items()
                            if key not in {"mode", "changed_reading_chapter_ids", "changed_stages"}}
                    for batch, scope in result["batches"].items()},
    }
