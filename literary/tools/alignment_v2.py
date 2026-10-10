"""Local input locks and append-only review references for the alignment gate.

These helpers do not read on behalf of reviewers, write literary decisions, or
promote observations.  A checkpoint is only a compatibility snapshot of an
already mechanically valid legacy workspace; it is not a new review receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from literary.tools import alignment as a


CHECKPOINT_NAME = "compatibility_checkpoint.json"
COUNTER_RECOMMENDATIONS = {"carry_forward", "refine", "reject", "ambiguous"}


def record_paths(directory: Path) -> list[Path]:
    """Only published JSONL files are inputs; pending files remain excluded."""
    return sorted(set(directory.glob("volume-*.jsonl")) | set(directory.glob("FA-V*.jsonl")))


def local_dependency_locks(
    context: a.Context,
    batch_id: str,
    issues: dict[str, dict[str, Any]],
    patches: dict[str, dict[str, Any]],
    reading_hashes: dict[str, str],
) -> dict[str, Any]:
    plan = context.batch_by_id[batch_id]
    target_ids = [row["chapter_id"] for row in plan["chapter_locks"]]
    positions = {int(chapter_id[2:]) - 1 + delta for chapter_id in target_ids for delta in (-1, 0, 1)}
    chapter_ids = [context.chapters[index]["chapter_id"] for index in sorted(positions)
                   if 0 <= index < len(context.chapters)]
    dependency_ids = set(chapter_ids)
    return {
        "chapter_locks": [
            {"chapter_id": chapter_id,
             "raw_chapter_sha256": context.chapter_by_id[chapter_id]["_raw_sha256"],
             "reading_chapter_sha256": reading_hashes[chapter_id]}
            for chapter_id in chapter_ids
        ],
        "issue_locks": [
            {"issue_id": issue_id, "record_sha256": row["record_sha256"]}
            for issue_id, row in sorted(issues.items())
            if dependency_ids.intersection({row.get("chapter_id"), *row.get("related_chapter_ids", [])})
        ],
        "patch_locks": [
            {"patch_id": patch_id, "record_sha256": row["record_sha256"]}
            for patch_id, row in sorted(patches.items()) if row.get("chapter_id") in dependency_ids
        ],
    }


def mechanical_inputs(context: a.Context, artifact_root: Path) -> tuple[dict, dict, dict, dict]:
    errors: list[str] = []
    issues = a._load_issues(context, artifact_root, errors)
    by_chapter, reading, _, projection = a._load_patches(context, artifact_root, issues, errors)
    if errors:
        raise a.AlignmentError("invalid alignment inputs:\n- " + "\n- ".join(errors))
    patches = {row["patch_id"]: row for rows in by_chapter.values() for row in rows}
    globals_ = {
        "source_sha256": context.manifest["source"]["sha256"],
        "issues_sha256": a._file_sha256_or_empty(artifact_root / "issues.jsonl"),
        "patches_sha256": a._file_sha256_or_empty(artifact_root / "patches.jsonl"),
        "reading_projection_sha256": hashlib.sha256(projection).hexdigest(),
    }
    return issues, patches, reading, globals_


def stage_input_locks(context: a.Context, batch_id: str, artifact_root: Path | None = None) -> dict:
    """Return mechanical inputs only, never a stage record or reading assertion."""
    if batch_id not in context.batch_by_id:
        raise a.AlignmentError(f"unknown batch_id: {batch_id}")
    artifact_root = (artifact_root or context.alignment_root).resolve()
    issues, patches, reading, _ = mechanical_inputs(context, artifact_root)
    plan = context.batch_by_id[batch_id]
    return {
        "schema_version": 2,
        "batch_id": batch_id,
        "input_manifest_sha256": context.manifest_file_sha256,
        "source_sha256": context.manifest["source"]["sha256"],
        "batch_index_record_sha256": plan["record_sha256"],
        "subject_sha256": plan["record_sha256"],
        "local_dependency_locks": local_dependency_locks(context, batch_id, issues, patches, reading),
    }


def load_checkpoint(context: a.Context, root: Path, errors: list[str]) -> dict | None:
    path = root / CHECKPOINT_NAME
    if not path.exists():
        return None
    checkpoint = a.load_json(path)
    a._check_record_hash(checkpoint, CHECKPOINT_NAME, errors)
    if checkpoint.get("schema_version") != 1 or checkpoint.get("record_type") != "mechanical_legacy_checkpoint":
        errors.append(f"{CHECKPOINT_NAME}: invalid checkpoint contract")
    if checkpoint.get("not_a_literary_review") is not True:
        errors.append(f"{CHECKPOINT_NAME}: mechanical-only scope is required")
    if checkpoint.get("input_manifest_sha256") != context.manifest_file_sha256:
        errors.append(f"{CHECKPOINT_NAME}: pinned manifest mismatch")
    if checkpoint.get("source_sha256") != context.manifest["source"]["sha256"]:
        errors.append(f"{CHECKPOINT_NAME}: immutable source mismatch")
    for field in ("artifact_locks", "stage_record_hashes", "observation_hashes", "legacy_observation_hashes", "batch_dependencies", "legacy_files"):
        if not isinstance(checkpoint.get(field), dict):
            raise a.AlignmentError(f"{CHECKPOINT_NAME}: {field} must be an object")
    for relative, expected in checkpoint["legacy_files"].items():
        path = a._repo_path(root, relative)
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"{CHECKPOINT_NAME}: frozen legacy file changed or missing: {relative}")
    return checkpoint


def validate_checkpoint_inventory(checkpoint: dict | None, stages: dict, observations: dict, errors: list[str]) -> None:
    if checkpoint is None:
        return
    actual = {f"{stage}/{batch_id}": row.get("record_sha256")
              for stage, rows in stages.items() for batch_id, row in rows.items()
              if row.get("schema_version") == 1}
    if actual != checkpoint["stage_record_hashes"]:
        errors.append(f"{CHECKPOINT_NAME}: legacy stage inventory/hash changed")
    actual_legacy_observations = {key: row.get("record_sha256") for key, row in observations.items()
                                  if row.get("schema_version", 1) == 1}
    if actual_legacy_observations != checkpoint["legacy_observation_hashes"]:
        errors.append(f"{CHECKPOINT_NAME}: legacy observation inventory/hash changed; new observations require schema 2")
    for observation_id, digest in checkpoint["observation_hashes"].items():
        if observations.get(observation_id, {}).get("record_sha256") != digest:
            errors.append(f"{CHECKPOINT_NAME}: frozen observation changed or missing: {observation_id}")


def checkpoint_legacy(context: a.Context, artifact_root: Path | None = None) -> Path:
    """Atomically publish once after validation, without rewriting any old data."""
    root = (artifact_root or context.alignment_root).resolve()
    target = root / CHECKPOINT_NAME
    before = _input_snapshot(context, root)
    report, errors = a.validate_alignment(context, root)
    if errors or not report["pass"]:
        raise a.AlignmentError("checkpoint requires a passing current check:\n- " + "\n- ".join(errors))
    if target.exists():
        return target  # validation above checked this exact existing checkpoint
    issues, patches, reading, globals_ = mechanical_inputs(context, root)
    stages: dict[str, str] = {}
    batch_ids: set[str] = set()
    legacy_files: dict[str, str] = {}
    for stage in a.STAGES:
        for path in record_paths(root / stage):
            rows = a.read_jsonl(path)
            if any(row.get("schema_version") == 1 for row in rows):
                legacy_files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            for row in rows:
                if row.get("schema_version") == 1:
                    stages[f"{stage}/{row['batch_id']}"] = row["record_sha256"]
                    batch_ids.add(row["batch_id"])
    observations: dict[str, str] = {}
    legacy_observations: dict[str, str] = {}
    for path in record_paths(root / "literary_observations"):
        legacy_files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        for row in a.read_jsonl(path):
            observations[row["observation_id"]] = row["record_sha256"]
            if row.get("schema_version", 1) == 1:
                legacy_observations[row["observation_id"]] = row["record_sha256"]
    checkpoint = a.with_record_hash({
        "schema_version": 1,
        "record_type": "mechanical_legacy_checkpoint",
        "not_a_literary_review": True,
        "input_manifest_sha256": context.manifest_file_sha256,
        "source_sha256": context.manifest["source"]["sha256"],
        "artifact_locks": globals_,
        "stage_record_hashes": stages,
        "observation_hashes": observations,
        "legacy_observation_hashes": legacy_observations,
        "legacy_files": legacy_files,
        "batch_dependencies": {batch_id: local_dependency_locks(context, batch_id, issues, patches, reading)
                               for batch_id in sorted(batch_ids)},
    })
    if _input_snapshot(context, root) != before:
        raise a.AlignmentError("alignment inputs changed during checkpoint creation; retry after writers stop")
    payload = json.dumps(checkpoint, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".checkpoint-", suffix=".pending", dir=root)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link publication is atomic and, unlike replace(), never overwrites
        # a competing checkpoint. Both paths are on the same local filesystem.
        try:
            os.link(temporary_path, target)
        except FileExistsError as exc:
            if target.read_bytes() != payload:
                raise a.AlignmentError("checkpoint already exists; refusing overwrite") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
    return target


def _input_snapshot(context: a.Context, root: Path) -> dict[str, str]:
    paths = {root / "batch_index.jsonl", root / "issues.jsonl", root / "patches.jsonl",
             a.reading_projection_path(context, root)}
    for directory in (*a.STAGES, "literary_observations"):
        paths.update(record_paths(root / directory))
    return {str(path): a._file_sha256_or_empty(path) for path in paths}


def validate_stage_inputs(row: dict, expected_local: dict, globals_: dict,
                          checkpoint: dict | None, label: str, errors: list[str]) -> None:
    if row.get("schema_version") == 2:
        if row.get("source_sha256") != globals_["source_sha256"]:
            errors.append(f"{label}: source_sha256 does not lock the current alignment input")
        if row.get("local_dependency_locks") != expected_local:
            errors.append(f"{label}: local_dependency_locks mismatch")
        return
    expected_globals = checkpoint["artifact_locks"] if checkpoint else globals_
    for field, digest in expected_globals.items():
        if row.get(field) != digest:
            errors.append(f"{label}: {field} does not lock the current alignment input")
    if checkpoint:
        key = f"{row.get('stage')}/{row.get('batch_id')}"
        if checkpoint["stage_record_hashes"].get(key) != row.get("record_sha256"):
            errors.append(f"{label}: legacy stage is not frozen in checkpoint")
        if checkpoint["batch_dependencies"].get(row.get("batch_id")) != expected_local:
            errors.append(f"{label}: checkpoint local_dependency_locks mismatch")


def expected_observations(observations: dict, chapter_id: str, stage: str) -> dict:
    rank = a.STAGES.index(stage)
    return {observation_id: row for observation_id, row in observations.items()
            if row.get("chapter_id") == chapter_id
            and (row.get("schema_version", 1) == 1
                 or (row.get("introduced_by_stage") in a.STAGES
                     and a.STAGES.index(row["introduced_by_stage"]) <= rank))}


def validate_observation_references(stages: dict, observations: dict, context: a.Context,
                                    errors: list[str], *, final: bool = False,
                                    historical_owners: dict | None = None) -> set[str]:
    """Bind actual observation versions; return v2 terminally routed IDs."""
    routed: set[str] = set()
    for observation_id, row in observations.items():
        if row.get("schema_version", 1) != 2:
            continue
        stage = row.get("introduced_by_stage")
        chapter_id = row.get("chapter_id")
        batch_id = context.chapter_to_batch.get(chapter_id)
        owner = stages.get(stage, {}).get(batch_id)
        # A release resolver may supply an independently validated ancestral
        # introducing record. This preserves authorship, never its approval.
        historical = (historical_owners or {}).get((observation_id, row.get("record_sha256")))
        historical_match = bool(
            historical and historical.get("schema_version") == 2
            and historical.get("stage") == stage and historical.get("batch_id") == batch_id
            and historical.get("task_id") == row.get("producer_task_id")
            and historical.get("record_sha256") == a.record_sha256(historical)
            and any(ch.get("chapter_id") == chapter_id and
                    {"observation_id": observation_id, "record_sha256": row.get("record_sha256")}
                    in ch.get("observation_locks", []) for ch in historical.get("chapters", []))
        )
        if not historical_match and ((owner and (owner.get("schema_version") != 2 or row.get("producer_task_id") != owner.get("task_id"))) or (final and not owner)):
            errors.append(f"{observation_id}: v2 observation requires its actual v2 introducing stage/task")
    for stage, batches in stages.items():
        for batch_id, record in batches.items():
            if record.get("schema_version") != 2:
                continue
            for chapter in record.get("chapters", []):
                if not isinstance(chapter, dict):
                    continue
                chapter_id = chapter.get("chapter_id")
                label = f"{batch_id}:{stage}:{chapter_id}"
                expected = expected_observations(observations, chapter_id, stage)
                expected_locks = [{"observation_id": key, "record_sha256": row["record_sha256"]}
                                  for key, row in sorted(expected.items())]
                if chapter.get("observation_locks") != expected_locks:
                    errors.append(f"{label}: observation_locks must exhaustively bind actual versions in ID order")
                if stage == "counter_review":
                    dispositions = chapter.get("observation_dispositions")
                    if not isinstance(dispositions, list):
                        continue
                    ids = [item.get("observation_id") for item in dispositions if isinstance(item, dict)]
                    if len(ids) != len(dispositions) or len(ids) != len(set(ids)) or set(ids) != set(expected):
                        errors.append(f"{label}: counter observation_dispositions must exhaustively cover observation_locks once")
                    for item in dispositions:
                        if not isinstance(item, dict) or item.get("recommendation") not in COUNTER_RECOMMENDATIONS or not a._has_content(item.get("rationale")):
                            errors.append(f"{label}: counter disposition requires recommendation and rationale")
                elif stage == "adjudication":
                    accepted = chapter.get("accepted_observation_ids", [])
                    rejected = chapter.get("rejected_observation_ids", [])
                    if isinstance(accepted, list) and isinstance(rejected, list):
                        if set(accepted + rejected) != set(expected):
                            errors.append(f"{label}: adjudication must route every locked observation")
                    dispositions = chapter.get("observation_dispositions")
                    if not isinstance(dispositions, list):
                        errors.append(f"{label}: adjudication observation_dispositions must be a list")
                        continue
                    ids = [item.get("observation_id") for item in dispositions if isinstance(item, dict)]
                    if len(ids) != len(dispositions) or len(ids) != len(set(ids)) or set(ids) != set(expected):
                        errors.append(f"{label}: adjudication dispositions must exhaustively cover observation_locks once")
                    for item in dispositions:
                        if not isinstance(item, dict):
                            errors.append(f"{label}: adjudication disposition must be an object")
                            continue
                        observation_id = item.get("observation_id")
                        disposition = item.get("handoff_disposition")
                        if item.get("record_sha256") != expected.get(observation_id, {}).get("record_sha256"):
                            errors.append(f"{label}: adjudication disposition observation version mismatch")
                        if disposition not in {"carry_forward", "rejected", "ambiguous"} or not a._has_content(item.get("rationale")):
                            errors.append(f"{label}: adjudication disposition requires terminal handoff_disposition and rationale")
                        elif (disposition == "rejected") != (observation_id in rejected):
                            errors.append(f"{label}: disposition disagrees with accepted/rejected observation IDs")
                        elif chapter.get("final_status") in a.TERMINAL_STATUSES:
                            routed.add(observation_id)
    return routed


def effective_observation_dispositions(stages: dict, observations: dict) -> dict[str, str]:
    """Project handoff states after a passing gate; these are NOT fact approvals."""
    result = {key: row.get("handoff_disposition", "pending") for key, row in observations.items()}
    for record in stages.get("adjudication", {}).values():
        if record.get("schema_version") != 2:
            continue
        for chapter in record.get("chapters", []):
            if chapter.get("final_status") in a.TERMINAL_STATUSES:
                for item in chapter.get("observation_dispositions", []):
                    result[item["observation_id"]] = item["handoff_disposition"]
    return result
