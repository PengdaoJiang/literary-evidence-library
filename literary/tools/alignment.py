#!/usr/bin/env python3
"""Small, fail-closed mechanical gate for Chinese full-text alignment.

This module never decides whether prose is correct or what it means.  It only
locks the immutable source, the deterministic reading plan, exact coordinates,
and the independence of the three review stages.

Record contract (schema version 1):

* one stage record covers one complete batch; ``chapters`` are subrecords;
* hashes use compact, key-sorted UTF-8 JSON with the top-level
  ``record_sha256`` field excluded;
* chapter hashes cover the literal source lines from ``source_line_title``
  through ``source_line_end``, joined with LF and with no trailing LF;
* raw and patched reading hashes are always separate;
* source lines are 1-based and ``line_char_range`` is 0-based, half-open;
* production and counter-review subrecords each list every actually read chunk
  in ``read_chunk_ids``; adjudication records carry the terminal status.
* an issue may be pending during ``check``, but a completed issue contains
  producer, counter-review and adjudication segments whose own canonical hashes
  form an explicit ``reviewed_*`` chain;
* approved patch cores are one-to-one with ``repair`` issue adjudications, and
  the reading projection is only raw source plus those mechanically closed
  patches.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
ALIGNMENT_REL = Path("literary/curation/alignment")
STAGES = ("production", "counter_review", "adjudication")
TERMINAL_STATUSES = {"aligned", "unresolvable"}
ALL_ADJUDICATION_STATUSES = TERMINAL_STATUSES | {"repair_required"}
OBSERVATION_DISPOSITIONS = {"pending", "carry_forward", "rejected", "ambiguous"}
HASH_RULE = "sha256(compact sorted UTF-8 JSON excluding top-level record_sha256)"
CHAPTER_HASH_SCOPE = (
    "UTF-8 SHA-256 of literal source lines source_line_title..source_line_end "
    "inclusive, joined by LF, without a trailing LF"
)
CHUNK_HASH_SCOPE = "UTF-8 SHA-256 of the normalized chunk text field"
MANIFEST_PLAN_HASH_SCOPE = (
    "SHA-256 of compact sorted UTF-8 manifest JSON after removing "
    "batching.batch_index_sha256"
)
STAGE_MANIFEST_HASH_SCOPE = "SHA-256 of the complete pinned manifest file bytes"
PRODUCTION_LIST_FIELDS = (
    "naming_findings",
    "location_findings",
    "faction_findings",
    "item_findings",
    "scene_findings",
    "setup_findings",
)
COUNTER_LIST_FIELDS = (
    "missed_findings",
    "overclaims",
    "identity_challenges",
    "continuity_or_boundary_challenges",
    "observation_dispositions",
)
OBSERVATION_TYPES = {
    "character_candidate",
    "creature_candidate",
    "faction_candidate",
    "location_candidate",
    "item_candidate",
    "scene_boundary_candidate",
    "setup_or_suspense_candidate",
    "payoff_candidate",
    "arc_beat_candidate",
    "identity_question",
    "text_issue_candidate",
}
HUMAN_TEXT_FIELDS = frozenset(
    {
        "candidate_label",
        "observation",
        "observation_type",
        "subkind",
        "does_not_claim",
        "narrative_digest",
        "continuity_in",
        "continuity_out",
        "finding",
        "rationale",
        "recommendation",
        "reason",
        "search_scope",
        "supersession_rationale",
        "competing_resolutions",
    }
) | frozenset(PRODUCTION_LIST_FIELDS) | frozenset(COUNTER_LIST_FIELDS)
ISSUE_DISPOSITIONS = {"repair", "no_repair", "unresolvable"}
PATCH_CORE_FIELDS = (
    "patch_id",
    "issue_id",
    "chapter_id",
    "source_line",
    "line_char_range",
    "raw_line_sha256",
    "before_exact",
    "after_exact",
)
EMPTY_LEDGER_SHA256 = hashlib.sha256(b"").hexdigest()


class AlignmentError(RuntimeError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AlignmentError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AlignmentError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AlignmentError(f"JSON root is not an object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line, object_pairs_hook=_unique_object)
                if not isinstance(value, dict):
                    raise AlignmentError(f"JSONL row is not an object: {path}:{line_number}")
                rows.append(value)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AlignmentError(f"cannot read JSONL {path}: {exc}") from exc
    return rows


def canonical_bytes(record: dict[str, Any]) -> bytes:
    payload = dict(record)
    payload.pop("record_sha256", None)
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def record_sha256(record: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(record)).hexdigest()


def with_record_hash(record: dict[str, Any]) -> dict[str, Any]:
    value = dict(record)
    value["record_sha256"] = record_sha256(value)
    return value


def patch_core_sha256(record: dict[str, Any]) -> str:
    core = {field: record.get(field) for field in PATCH_CORE_FIELDS}
    encoded = json.dumps(
        core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256_or_empty(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else EMPTY_LEDGER_SHA256


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repo_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise AlignmentError(f"path escapes workspace: {relative}") from exc
    return path


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AlignmentError(message)


@dataclass
class Context:
    root: Path
    alignment_root: Path
    manifest: dict[str, Any]
    source_lines: list[str]
    chapters: list[dict[str, Any]]
    chapter_by_id: dict[str, dict[str, Any]]
    chunks_by_chapter: dict[str, list[dict[str, Any]]]
    chunk_text: dict[str, str]
    batches: list[dict[str, Any]]
    batch_index: list[dict[str, Any]]
    batch_by_id: dict[str, dict[str, Any]]
    chapter_to_batch: dict[str, str]
    manifest_file_sha256: str
    manifest_plan_sha256: str


def _is_read_only(path: Path) -> bool:
    info = path.stat()
    attributes = getattr(info, "st_file_attributes", 0)
    if attributes:
        return bool(attributes & stat.FILE_ATTRIBUTE_READONLY)
    return not bool(info.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def manifest_plan_sha256(manifest: dict[str, Any]) -> str:
    """Hash the plan input without the manifest's reciprocal batch-index pin."""
    payload = copy.deepcopy(manifest)
    payload.get("batching", {}).pop("batch_index_sha256", None)
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compute_batches(
    chapters: list[dict[str, Any]], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    rules = manifest["batching"]
    max_chapters = rules["max_chapters"]
    max_chars = rules["max_chapter_body_chars"]
    batches: list[dict[str, Any]] = []
    volume_order = {
        row["volume_title"]: index for index, row in enumerate(manifest["volume_ranges"])
    }
    volume_batch_counts = {title: 0 for title in volume_order}
    current: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        volume_title = current[0]["volume_title"]
        volume_batch_counts[volume_title] += 1
        batches.append(
            {
                "batch_id": (
                    f"FA-V{volume_order[volume_title]:02d}-"
                    f"{volume_batch_counts[volume_title]:02d}"
                ),
                "volume_title": volume_title,
                "chapter_ids": [row["chapter_id"] for row in current],
                "first_chapter_id": current[0]["chapter_id"],
                "last_chapter_id": current[-1]["chapter_id"],
                "chapter_count": len(current),
                "chapter_body_char_count": current_chars,
            }
        )
        current = []
        current_chars = 0

    for chapter in chapters:
        size = chapter["char_count"]
        _require(size <= max_chars, f"single chapter exceeds batch character limit: {chapter['chapter_id']}")
        if current and (
            chapter["volume_title"] != current[0]["volume_title"]
            or len(current) >= max_chapters
            or current_chars + size > max_chars
        ):
            flush()
        current.append(chapter)
        current_chars += size
    flush()
    return batches


def _neighbor_lock(chapters: list[dict[str, Any]], index: int) -> dict[str, str] | None:
    if index < 0 or index >= len(chapters):
        return None
    row = chapters[index]
    return {"chapter_id": row["chapter_id"], "raw_chapter_sha256": row["_raw_sha256"]}


def _make_batch_index(
    batches: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
    chunks: dict[str, list[dict[str, Any]]],
    volume_markers: dict[str, dict[str, Any]],
    input_manifest_sha256: str,
) -> list[dict[str, Any]]:
    chapter_position = {row["chapter_id"]: index for index, row in enumerate(chapters)}
    chapter_by_id = {row["chapter_id"]: row for row in chapters}
    output: list[dict[str, Any]] = []
    seen_volumes: set[str] = set()
    for batch in batches:
        first_pos = chapter_position[batch["first_chapter_id"]]
        last_pos = chapter_position[batch["last_chapter_id"]]
        locks = []
        for chapter_id in batch["chapter_ids"]:
            row = chapter_by_id[chapter_id]
            locks.append(
                {
                    "chapter_id": chapter_id,
                    "raw_chapter_sha256": row["_raw_sha256"],
                    "chunk_locks": [
                        {"chunk_id": chunk["chunk_id"], "text_sha256": chunk["text_sha256"]}
                        for chunk in chunks[chapter_id]
                    ],
                }
            )
        marker_lock = (
            volume_markers[batch["volume_title"]]
            if batch["volume_title"] not in seen_volumes
            else None
        )
        seen_volumes.add(batch["volume_title"])
        output.append(
            with_record_hash(
                {
                    "schema_version": 1,
                    "record_type": "alignment_batch_plan",
                    "batch_id": batch["batch_id"],
                    "volume_title": batch["volume_title"],
                    "first_chapter_id": batch["first_chapter_id"],
                    "last_chapter_id": batch["last_chapter_id"],
                    "chapter_count": batch["chapter_count"],
                    "chapter_body_char_count": batch["chapter_body_char_count"],
                    "hash_basis": {
                        "record_sha256": HASH_RULE,
                        "raw_chapter_sha256": CHAPTER_HASH_SCOPE,
                        "chunk_text_sha256": CHUNK_HASH_SCOPE,
                        "input_manifest_sha256": MANIFEST_PLAN_HASH_SCOPE,
                    },
                    "input_manifest_sha256": input_manifest_sha256,
                    "volume_marker_lock": marker_lock,
                    "ordered_chapter_ids": list(batch["chapter_ids"]),
                    "ordered_chunk_ids": [
                        chunk["chunk_id"]
                        for chapter_id in batch["chapter_ids"]
                        for chunk in chunks[chapter_id]
                    ],
                    "chapter_locks": locks,
                    "previous_chapter_lock": _neighbor_lock(chapters, first_pos - 1),
                    "next_chapter_lock": _neighbor_lock(chapters, last_pos + 1),
                }
            )
        )
    return output


def load_context(root: Path = ROOT, alignment_root: Path | None = None) -> Context:
    root = root.resolve()
    alignment_root = (alignment_root or root / ALIGNMENT_REL).resolve()
    manifest_path = alignment_root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest_file_digest = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = load_json(manifest_path)
    manifest_plan_digest = manifest_plan_sha256(manifest)
    _require(manifest.get("schema_version") == 2, "alignment manifest schema_version must be 2")

    source_spec = manifest["source"]
    source_path = _repo_path(root, source_spec["path"])
    source_bytes = source_path.read_bytes()
    _require(hashlib.sha256(source_bytes).hexdigest() == source_spec["sha256"], "source SHA-256 mismatch")
    _require(len(source_bytes) == source_spec["size_bytes"], "source byte size mismatch")
    _require(source_bytes.startswith(b"\xef\xbb\xbf") == source_spec["bom"], "source BOM mismatch")
    source_text = source_bytes.decode(source_spec["encoding"])
    source_lines = source_text.splitlines()
    _require(len(source_lines) == source_spec["line_count"], "source line count mismatch")
    if source_spec.get("read_only"):
        _require(_is_read_only(source_path), "source file is not read-only")

    topology = manifest["topology"]
    chapter_index_path = _repo_path(root, topology["chapter_index_path"])
    chunk_index_path = _repo_path(root, topology["chunk_index_path"])
    _require(
        hashlib.sha256(chapter_index_path.read_bytes()).hexdigest()
        == topology["chapter_index_sha256"],
        "chapter index SHA-256 mismatch",
    )
    _require(
        hashlib.sha256(chunk_index_path.read_bytes()).hexdigest()
        == topology["chunk_index_sha256"],
        "chunk index SHA-256 mismatch",
    )
    chapter_rows = read_jsonl(chapter_index_path)
    _require(len(chapter_rows) == topology["chapter_count"] and len(chapter_rows) > 0, "chapter inventory differs from the manifest")
    expected_ids = [f"ch{number:04d}" for number in range(1, len(chapter_rows) + 1)]
    _require([row.get("chapter_id") for row in chapter_rows] == expected_ids, "chapter IDs are not sequential")
    _require([row.get("chapter_number") for row in chapter_rows] == list(range(1, len(chapter_rows) + 1)), "chapter numbers are not sequential")

    marker_by_title: dict[str, dict[str, Any]] = {}
    for marker in manifest["volume_markers"]:
        title = marker.get("volume_title")
        line_number = marker.get("source_line")
        _require(isinstance(title, str) and title not in marker_by_title, "invalid or duplicate volume marker")
        _require(isinstance(line_number, int) and 1 <= line_number <= len(source_lines), f"invalid volume marker line: {title}")
        literal = source_lines[line_number - 1]
        _require(literal == marker.get("literal_text"), f"volume marker literal mismatch: {title}")
        _require(_sha256_text(literal) == marker.get("sha256"), f"volume marker hash mismatch: {title}")
        marker_by_title[title] = dict(marker)
    _require(len(marker_by_title) == topology["volume_count"], "volume marker count mismatch")

    first_ids = {item["first_chapter_id"] for item in manifest["volume_ranges"]}
    for row in chapter_rows:
        chapter_id = row["chapter_id"]
        _require(source_lines[row["source_line_title"] - 1].strip() == row["title"], f"title mismatch: {chapter_id}")
        body = "\n".join(source_lines[row["source_line_body_start"] - 1 : row["source_line_end"]]).rstrip()
        _require(len(body.replace("\n", "")) == row["char_count"], f"body char count mismatch: {chapter_id}")
        _require(sum(bool(line.strip()) for line in body.splitlines()) == row["paragraph_count"], f"paragraph count mismatch: {chapter_id}")
        unit_start = row["source_line_title"]
        unit_text = "\n".join(source_lines[unit_start - 1 : row["source_line_end"]])
        row["_unit_start"] = unit_start
        row["_unit_end"] = row["source_line_end"]
        row["_patch_start"] = (
            marker_by_title[row["volume_title"]]["source_line"]
            if chapter_id in first_ids
            else unit_start
        )
        row["_raw_sha256"] = _sha256_text(unit_text)

    _require(sum(row["char_count"] for row in chapter_rows) == topology["chapter_body_char_count"], "chapter character total mismatch")
    _require(sum(row["paragraph_count"] for row in chapter_rows) == topology["body_paragraph_count"], "paragraph total mismatch")
    _require(len(manifest["volume_ranges"]) == topology["volume_count"], "volume inventory differs from the manifest")
    chapter_by_id = {row["chapter_id"]: row for row in chapter_rows}
    for volume in manifest["volume_ranges"]:
        start = int(volume["first_chapter_id"][2:]) - 1
        end = int(volume["last_chapter_id"][2:])
        rows = chapter_rows[start:end]
        _require(len(rows) == volume["chapter_count"], f"volume chapter count mismatch: {volume['volume_title']}")
        _require(all(row["volume_title"] == volume["volume_title"] for row in rows), f"volume boundary mismatch: {volume['volume_title']}")
        _require(rows[0]["chapter_id"] == volume["first_chapter_id"] and rows[-1]["chapter_id"] == volume["last_chapter_id"], f"volume range mismatch: {volume['volume_title']}")

    chunk_rows = read_jsonl(chunk_index_path)
    _require(len(chunk_rows) == topology["chunk_count"], "chunk inventory differs from the manifest")
    chunks_by_chapter: dict[str, list[dict[str, Any]]] = {chapter_id: [] for chapter_id in expected_ids}
    chunk_text: dict[str, str] = {}
    seen_chunks: set[str] = set()
    for row in chunk_rows:
        chunk_id = row.get("chunk_id")
        chapter_id = row.get("chapter_id")
        _require(isinstance(chunk_id, str) and chunk_id not in seen_chunks, f"duplicate or invalid chunk: {chunk_id}")
        _require(chapter_id in chapter_by_id, f"chunk has unknown chapter: {chunk_id}")
        payload = load_json(_repo_path(root, row["path"]))
        text = payload.get("text")
        _require(payload.get("chunk_id") == chunk_id and isinstance(text, str), f"chunk payload mismatch: {chunk_id}")
        digest = _sha256_text(text)
        _require(digest == row.get("text_sha256") == payload.get("text_sha256"), f"chunk text hash mismatch: {chunk_id}")
        _require(row.get("volume_title") == chapter_by_id[chapter_id]["volume_title"], f"chunk volume mismatch: {chunk_id}")
        seen_chunks.add(chunk_id)
        chunks_by_chapter[chapter_id].append(row)
        chunk_text[chunk_id] = text
    for chapter_id, rows in chunks_by_chapter.items():
        rows.sort(key=lambda row: row["chunk_index"])
        _require(bool(rows), f"chapter has no chunks: {chapter_id}")
        _require([row["chunk_index"] for row in rows] == list(range(1, len(rows) + 1)), f"chunk sequence mismatch: {chapter_id}")
        _require([row["chunk_id"] for row in rows] == [f"{chapter_id}_ck{index:04d}" for index in range(1, len(rows) + 1)], f"chunk ID sequence mismatch: {chapter_id}")

    batches = _compute_batches(chapter_rows, manifest)
    rules = manifest["batching"]
    _require(len(batches) == rules["expected_batch_count"], "batch inventory differs from the manifest")
    max_chars = max(batch["chapter_body_char_count"] for batch in batches)
    _require(max_chars == rules["observed_max_batch_chars"], "observed maximum batch size mismatch")
    batch_index = _make_batch_index(
        batches, chapter_rows, chunks_by_chapter, marker_by_title, manifest_plan_digest
    )
    generated_batch_digest = hashlib.sha256(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for row in batch_index
        ).encode("utf-8")
    ).hexdigest()
    pinned_batch_digest = rules.get("batch_index_sha256")
    if pinned_batch_digest is not None:
        _require(
            generated_batch_digest == pinned_batch_digest,
            "deterministic batch index SHA-256 does not match manifest pin",
        )
    batch_by_id = {row["batch_id"]: row for row in batch_index}
    chapter_to_batch = {
        lock["chapter_id"]: row["batch_id"] for row in batch_index for lock in row["chapter_locks"]
    }
    return Context(
        root, alignment_root, manifest, source_lines, chapter_rows, chapter_by_id,
        chunks_by_chapter, chunk_text, batches, batch_index, batch_by_id, chapter_to_batch,
        manifest_file_digest, manifest_plan_digest,
    )


def write_batch_index(context: Context, alignment_root: Path | None = None) -> Path:
    alignment_root = (alignment_root or context.alignment_root).resolve()
    alignment_root.mkdir(parents=True, exist_ok=True)
    path = alignment_root / "batch_index.jsonl"
    data = batch_index_bytes(context)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return path


def batch_index_bytes(context: Context) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in context.batch_index
    ).encode("utf-8")


def reading_projection_path(context: Context, artifact_root: Path | None = None) -> Path:
    artifact_root = (artifact_root or context.alignment_root).resolve()
    if artifact_root == context.alignment_root:
        return _repo_path(
            context.root, context.manifest["artifact_paths"]["generated_reading_text"]
        )
    return artifact_root / "reading_projection.txt"


def write_reading_projection(
    context: Context, projection: bytes, artifact_root: Path | None = None
) -> Path:
    path = reading_projection_path(context, artifact_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(projection)
    os.replace(temporary, path)
    return path


def _check_record_hash(row: dict[str, Any], label: str, errors: list[str]) -> None:
    actual = row.get("record_sha256")
    expected = record_sha256(row)
    if actual != expected:
        errors.append(f"{label}: record_sha256 mismatch ({HASH_RULE})")


def _looks_garbled_human_text(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if any(marker in text for marker in ("\ufffd", "\u951f\u65a4\u62f7", "\u00ef\u00bf\u00bd")):
        return True
    if "???" in text:
        return True
    return "?" in text and all(character == "?" or character.isspace() for character in text)


def _check_human_text(value: Any, label: str, errors: list[str]) -> None:
    if isinstance(value, str):
        if _looks_garbled_human_text(value):
            errors.append(
                f"{label}: human-readable text contains a replacement or garbled placeholder"
            )
    elif isinstance(value, list):
        for index, item in enumerate(value, 1):
            _check_human_text(item, f"{label}[{index}]", errors)
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_human_text(item, f"{label}.{key}", errors)


def _check_named_human_text_fields(
    value: Any, label: str, errors: list[str]
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            item_label = f"{label}:{key}"
            if key in HUMAN_TEXT_FIELDS:
                _check_human_text(item, item_label, errors)
            elif isinstance(item, (dict, list)):
                _check_named_human_text_fields(item, item_label, errors)
    elif isinstance(value, list):
        for index, item in enumerate(value, 1):
            _check_named_human_text_fields(item, f"{label}[{index}]", errors)


def _load_issues(context: Context, artifact_root: Path, errors: list[str]) -> dict[str, dict[str, Any]]:
    path = artifact_root / "issues.jsonl"
    if not path.exists():
        return {}
    rows = read_jsonl(path)
    issues: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, 1):
        label = f"issues.jsonl:{index}"
        _check_record_hash(row, label, errors)
        _check_named_human_text_fields(row, label, errors)
        issue_id = row.get("issue_id")
        if not isinstance(issue_id, str) or not issue_id or issue_id in issues:
            errors.append(f"{label}: invalid or duplicate issue_id")
            continue
        if row.get("chapter_id") not in context.chapter_by_id:
            errors.append(f"{label}: unknown chapter_id")
        if row.get("schema_version") != 1 or row.get("record_type") != "alignment_issue":
            errors.append(f"{label}: invalid issue record contract")

        related_chapter_ids = row.get("related_chapter_ids", [])
        if not (
            isinstance(related_chapter_ids, list)
            and len(related_chapter_ids) == len(set(related_chapter_ids))
            and all(chapter_id in context.chapter_by_id for chapter_id in related_chapter_ids)
        ):
            errors.append(f"{label}: related_chapter_ids must be unique known chapters")
            related_chapter_ids = []
        allowed_chapter_ids = {row.get("chapter_id"), *related_chapter_ids}
        source_anchors = row.get("source_anchors")
        supporting_anchors = row.get("supporting_source_anchors", [])
        if not isinstance(supporting_anchors, list):
            errors.append(f"{label}: supporting_source_anchors must be a list")
            supporting_anchors = []
        if not isinstance(source_anchors, list) or not source_anchors:
            errors.append(f"{label}: source_anchors must be a non-empty list")
        else:
            for anchor_index, anchor in enumerate(source_anchors + supporting_anchors, 1):
                supporting = anchor_index > len(source_anchors)
                anchor_label = f"{label}:{'supporting_' if supporting else ''}source_anchor-{anchor_index}"
                if not isinstance(anchor, dict):
                    errors.append(f"{anchor_label}: source anchor must be an object")
                    continue
                if supporting:
                    expected_fields = {"chapter_id", "reading_layer", "source_line", "line_char_range",
                                       "raw_line_sha256", "before_exact"}
                    if set(anchor) != expected_fields:
                        errors.append(f"{anchor_label}: supporting source anchor has unexpected or missing fields")
                    support_range = anchor.get("line_char_range")
                    if (type(anchor.get("source_line")) is not int or not isinstance(support_range, list)
                            or len(support_range) != 2 or any(type(value) is not int for value in support_range)):
                        errors.append(f"{anchor_label}: supporting coordinates require integer line and offsets, not booleans")
                anchor_chapter_id = anchor.get("chapter_id", None if supporting else row.get("chapter_id"))
                if supporting and anchor.get("reading_layer") != "diplomatic":
                    errors.append(f"{anchor_label}: supporting evidence must explicitly use the immutable diplomatic layer")
                anchor_allowed = context.chapter_by_id if supporting else allowed_chapter_ids
                if anchor_chapter_id not in anchor_allowed:
                    errors.append(
                        f"{anchor_label}: chapter_id must be the issue chapter or a related chapter"
                    )
                    continue
                chapter = context.chapter_by_id.get(anchor_chapter_id)
                source_line = anchor.get("source_line")
                char_range = anchor.get("line_char_range")
                if not isinstance(source_line, int) or not chapter or not (
                    chapter["source_line_title"] <= source_line <= chapter["_unit_end"]
                ):
                    errors.append(f"{anchor_label}: source_line is outside the anchored chapter")
                    continue
                if not (
                    isinstance(char_range, list)
                    and len(char_range) == 2
                    and all(isinstance(value, int) for value in char_range)
                ):
                    errors.append(f"{anchor_label}: line_char_range must be [start,end]")
                    continue
                start, end = char_range
                raw_line = context.source_lines[source_line - 1]
                if not (0 <= start < end <= len(raw_line)):
                    errors.append(f"{anchor_label}: line_char_range must be non-empty and half-open")
                    continue
                if anchor.get("raw_line_sha256") != _sha256_text(raw_line):
                    errors.append(f"{anchor_label}: raw line hash mismatch")
                if anchor.get("before_exact") != raw_line[start:end]:
                    errors.append(f"{anchor_label}: before_exact does not match the immutable source")

        producer = row.get("producer")
        counter = row.get("counter_review")
        adjudication = row.get("adjudication")
        confirmed_repair = any(
            isinstance(segment, dict) and segment.get("decision") == decision
            for segment, decision in (
                (producer, "propose_repair"),
                (counter, "pass_repair"),
                (adjudication, "approve_repair"),
            )
        )
        if not isinstance(producer, dict):
            errors.append(f"{label}: producer segment is required")
        if confirmed_repair:
            if isinstance(producer, dict):
                _check_record_hash(producer, f"{label}:producer", errors)
                for field in ("task_id", "patch_core_sha256", "rationale_code"):
                    if not _has_content(producer.get(field)):
                        errors.append(f"{label}:producer: {field} must be non-empty")
                if producer.get("decision") != "propose_repair":
                    errors.append(f"{label}: confirmed repair producer decision mismatch")
                if not _is_sha256(producer.get("patch_core_sha256")):
                    errors.append(f"{label}: producer patch_core_sha256 is invalid")
            if counter is not None:
                if not isinstance(counter, dict):
                    errors.append(f"{label}: counter_review must be an object")
                else:
                    _check_record_hash(counter, f"{label}:counter_review", errors)
                    for field in ("task_id", "patch_core_sha256", "rationale_code"):
                        if not _has_content(counter.get(field)):
                            errors.append(f"{label}:counter_review: {field} must be non-empty")
                    if counter.get("decision") != "pass_repair":
                        errors.append(f"{label}: confirmed repair counter decision mismatch")
                    if not isinstance(producer, dict) or counter.get(
                        "producer_sha256"
                    ) != producer.get("record_sha256"):
                        errors.append(f"{label}: counter_review producer hash mismatch")
                    if not isinstance(producer, dict) or counter.get(
                        "patch_core_sha256"
                    ) != producer.get("patch_core_sha256"):
                        errors.append(f"{label}: counter_review patch core mismatch")
            if adjudication is not None:
                if not isinstance(adjudication, dict):
                    errors.append(f"{label}: adjudication must be an object")
                else:
                    _check_record_hash(adjudication, f"{label}:adjudication", errors)
                    for field in ("task_id", "approved_patch_sha256", "rationale_code"):
                        if not _has_content(adjudication.get(field)):
                            errors.append(f"{label}:adjudication: {field} must be non-empty")
                    if adjudication.get("decision") != "approve_repair":
                        errors.append(f"{label}: confirmed repair adjudication decision mismatch")
                    if not isinstance(producer, dict) or adjudication.get(
                        "producer_sha256"
                    ) != producer.get("record_sha256"):
                        errors.append(f"{label}: adjudication producer hash mismatch")
                    if not isinstance(counter, dict) or adjudication.get(
                        "counter_review_sha256"
                    ) != counter.get("record_sha256"):
                        errors.append(f"{label}: adjudication counter-review hash mismatch")
                    if not isinstance(producer, dict) or adjudication.get(
                        "approved_patch_sha256"
                    ) != producer.get("patch_core_sha256"):
                        errors.append(f"{label}: adjudication approved patch mismatch")
        else:
            if isinstance(producer, dict):
                _check_record_hash(producer, f"{label}:producer", errors)
                for field in ("task_id", "finding", "rationale"):
                    if not _has_content(producer.get(field)):
                        errors.append(f"{label}:producer: {field} must be non-empty")
            if counter is not None:
                if not isinstance(counter, dict):
                    errors.append(f"{label}: counter_review must be an object")
                else:
                    _check_record_hash(counter, f"{label}:counter_review", errors)
                    for field in ("task_id", "recommendation", "rationale"):
                        if not _has_content(counter.get(field)):
                            errors.append(f"{label}:counter_review: {field} must be non-empty")
                    if not isinstance(producer, dict) or counter.get(
                        "reviewed_producer_sha256"
                    ) != producer.get("record_sha256"):
                        errors.append(f"{label}: counter_review producer hash mismatch")
            if adjudication is not None:
                if not isinstance(adjudication, dict):
                    errors.append(f"{label}: adjudication must be an object")
                else:
                    _check_record_hash(adjudication, f"{label}:adjudication", errors)
                    for field in ("task_id", "disposition", "rationale"):
                        if not _has_content(adjudication.get(field)):
                            errors.append(f"{label}:adjudication: {field} must be non-empty")
                    if adjudication.get("disposition") not in {
                        "no_repair", "unresolvable"
                    }:
                        errors.append(
                            f"{label}: generic issue adjudication must be non-repair"
                        )
                    if adjudication.get("approved_patch_sha256") is not None:
                        errors.append(
                            f"{label}: non-repair adjudication must not approve a patch"
                        )
                    if adjudication.get("disposition") == "unresolvable" and not (
                        isinstance(adjudication.get("competing_resolutions"), list)
                        and len(adjudication["competing_resolutions"]) >= 2
                        and _has_content(adjudication.get("search_scope"))
                    ):
                        errors.append(
                            f"{label}: unresolvable adjudication requires at least two "
                            "competing resolutions and a search_scope"
                        )
                    if not isinstance(producer, dict) or adjudication.get(
                        "reviewed_producer_sha256"
                    ) != producer.get("record_sha256"):
                        errors.append(f"{label}: adjudication producer hash mismatch")
                    if not isinstance(counter, dict) or adjudication.get(
                        "reviewed_counter_review_sha256"
                    ) != counter.get("record_sha256"):
                        errors.append(f"{label}: adjudication counter-review hash mismatch")

        task_ids = [
            segment.get("task_id")
            for segment in (producer, counter, adjudication)
            if isinstance(segment, dict) and segment.get("task_id") is not None
        ]
        if len(task_ids) != len(set(task_ids)):
            errors.append(
                f"{label}: producer, counter_review and adjudication task_id values must be distinct"
            )
        row["_preflight_state"] = (
            "completed"
            if all(isinstance(segment, dict) for segment in (producer, counter, adjudication))
            else "pending"
        )
        row["_preflight_disposition"] = (
            "repair"
            if confirmed_repair and row["_preflight_state"] == "completed"
            else adjudication.get("disposition")
            if isinstance(adjudication, dict) and row["_preflight_state"] == "completed"
            else None
        )
        issues[issue_id] = row
    return issues


def _load_patches(
    context: Context, artifact_root: Path, issues: dict[str, dict[str, Any]], errors: list[str]
) -> tuple[
    dict[str, list[dict[str, Any]]], dict[str, str], dict[str, list[str]], bytes
]:
    path = artifact_root / "patches.jsonl"
    patches_by_chapter: dict[str, list[dict[str, Any]]] = {chapter_id: [] for chapter_id in context.chapter_by_id}
    if path.exists():
        seen: set[str] = set()
        for index, row in enumerate(read_jsonl(path), 1):
            label = f"patches.jsonl:{index}"
            _check_record_hash(row, label, errors)
            _check_named_human_text_fields(row, label, errors)
            patch_id = row.get("patch_id")
            chapter_id = row.get("chapter_id")
            required_strings = (
                "issue_id", "before_exact", "after_exact", "raw_line_sha256", "reason",
                "adjudication_record_sha256", "approved_patch_sha256",
            )
            if not isinstance(patch_id, str) or not patch_id or patch_id in seen:
                errors.append(f"{label}: invalid or duplicate patch_id")
                continue
            seen.add(patch_id)
            if chapter_id not in context.chapter_by_id:
                errors.append(f"{label}: unknown chapter_id")
                continue
            if any(not isinstance(row.get(key), str) for key in required_strings):
                errors.append(f"{label}: missing patch string fields")
                continue
            if row["approved_patch_sha256"] != patch_core_sha256(row):
                errors.append(f"{label}: approved_patch_sha256 does not match the canonical patch core")
            if row.get("schema_version") != 1 or row.get("record_type") != "alignment_patch":
                errors.append(f"{label}: invalid patch record contract")
            issue = issues.get(row["issue_id"])
            if issue is None:
                errors.append(f"{label}: patch issue_id is not present in issues.jsonl")
            else:
                adjudication = issue.get("adjudication")
                if issue.get("_preflight_state") != "completed" or not isinstance(adjudication, dict):
                    errors.append(f"{label}: approved patch requires a completed issue adjudication")
                else:
                    if issue.get("_preflight_disposition") != "repair":
                        errors.append(f"{label}: only a repair issue may have an approved patch")
                    if row["chapter_id"] != issue.get("chapter_id"):
                        errors.append(f"{label}: patch and issue must belong to the same chapter")
                    if row["adjudication_record_sha256"] != adjudication.get("record_sha256"):
                        errors.append(f"{label}: adjudication_record_sha256 does not lock the issue adjudication")
                    if row["approved_patch_sha256"] != adjudication.get("approved_patch_sha256"):
                        errors.append(f"{label}: patch hash does not match the issue adjudication approval")
            if row.get("status") != "approved":
                errors.append(f"{label}: patches.jsonl accepts only approved patches")
            source_line = row.get("source_line")
            char_range = row.get("line_char_range")
            chapter = context.chapter_by_id[chapter_id]
            if not isinstance(source_line, int) or not chapter["source_line_title"] <= source_line <= chapter["_unit_end"]:
                errors.append(
                    f"{label}: source_line must be 1-based and at or after the chapter title; "
                    "volume-marker patches are forbidden"
                )
                continue
            if not (
                isinstance(char_range, list) and len(char_range) == 2
                and all(isinstance(value, int) for value in char_range)
            ):
                errors.append(f"{label}: line_char_range must be [start,end]")
                continue
            start, end = char_range
            raw_line = context.source_lines[source_line - 1]
            if not (0 <= start <= end <= len(raw_line)):
                errors.append(f"{label}: line_char_range must be 0-based half-open")
                continue
            if _sha256_text(raw_line) != row["raw_line_sha256"]:
                errors.append(f"{label}: raw line hash mismatch")
            if raw_line[start:end] != row["before_exact"]:
                errors.append(f"{label}: before_exact does not exactly match the immutable source line")
            if row["after_exact"] == row["before_exact"]:
                errors.append(f"{label}: after_exact must differ from before_exact (no-op patch)")
            patches_by_chapter[chapter_id].append(row)

    patches_by_issue: dict[str, list[dict[str, Any]]] = {}
    for rows in patches_by_chapter.values():
        for row in rows:
            patches_by_issue.setdefault(row["issue_id"], []).append(row)
    for issue_id, issue in issues.items():
        linked = patches_by_issue.get(issue_id, [])
        adjudication = issue.get("adjudication")
        if issue.get("_preflight_state") != "completed" or not isinstance(adjudication, dict):
            if linked:
                errors.append(f"issues.jsonl:{issue_id}: pending issue must have zero approved patches")
            continue
        disposition = issue.get("_preflight_disposition")
        if disposition == "repair":
            if len(linked) != 1:
                errors.append(
                    f"issues.jsonl:{issue_id}: repair issue must have exactly one approved patch"
                )
        else:
            if linked:
                errors.append(
                    f"issues.jsonl:{issue_id}: {disposition} issue must have zero approved patches"
                )
            if adjudication.get("approved_patch_sha256") is not None:
                errors.append(
                    f"issues.jsonl:{issue_id}: non-repair adjudication must not approve a patch"
                )

    reading_lines = list(context.source_lines)
    applied_ids: dict[str, list[str]] = {chapter_id: [] for chapter_id in context.chapter_by_id}
    by_line: dict[int, list[dict[str, Any]]] = {}
    for chapter_id, rows in patches_by_chapter.items():
        for row in rows:
            if isinstance(row.get("source_line"), int) and isinstance(row.get("line_char_range"), list):
                by_line.setdefault(row["source_line"], []).append(row)
                applied_ids[chapter_id].append(row["patch_id"])
        applied_ids[chapter_id].sort()
    for source_line, rows in by_line.items():
        ordered = sorted(rows, key=lambda row: tuple(row["line_char_range"]))
        for previous, current in zip(ordered, ordered[1:]):
            p_start, p_end = previous["line_char_range"]
            c_start, c_end = current["line_char_range"]
            if c_start < p_end or (c_start == p_start and c_end == p_end):
                errors.append(f"patches.jsonl: overlapping raw coordinates on source line {source_line}")
        line = reading_lines[source_line - 1]
        for row in reversed(ordered):
            start, end = row["line_char_range"]
            line = line[:start] + row["after_exact"] + line[end:]
        reading_lines[source_line - 1] = line

    reading_hashes = {
        chapter_id: _sha256_text("\n".join(reading_lines[row["_unit_start"] - 1 : row["_unit_end"]]))
        for chapter_id, row in context.chapter_by_id.items()
    }
    projection = "\n".join(reading_lines).encode("utf-8")
    return patches_by_chapter, reading_hashes, applied_ids, projection


def generate_reading_projection(
    context: Context, artifact_root: Path | None = None
) -> Path:
    """Write only the projection mechanically implied by raw source and closed patches."""
    artifact_root = (artifact_root or context.alignment_root).resolve()
    errors: list[str] = []
    issues = _load_issues(context, artifact_root, errors)
    _, _, _, projection = _load_patches(context, artifact_root, issues, errors)
    if errors:
        raise AlignmentError("cannot generate reading projection:\n- " + "\n- ".join(errors))
    return write_reading_projection(context, projection, artifact_root)


def _reading_neighbor_lock(
    context: Context, reading_hashes: dict[str, str], chapter_id: str, delta: int
) -> dict[str, str] | None:
    position = int(chapter_id[2:]) - 1 + delta
    if not 0 <= position < len(context.chapters):
        return None
    row = context.chapters[position]
    return {
        "chapter_id": row["chapter_id"],
        "raw_chapter_sha256": row["_raw_sha256"],
        "reading_chapter_sha256": reading_hashes[row["chapter_id"]],
    }


def _validate_chunk_anchor(
    context: Context,
    anchor: dict[str, Any],
    label: str,
    errors: list[str],
    expected_chapter_id: str | None = None,
) -> bool:
    chunk_id = anchor.get("chunk_id")
    if chunk_id not in context.chunk_text:
        errors.append(f"{label}: unknown chunk_id")
        return False
    text = context.chunk_text[chunk_id]
    actual_chapter_id = chunk_id.split("_", 1)[0]
    if anchor.get("chapter_id", actual_chapter_id) != actual_chapter_id:
        errors.append(f"{label}: chapter/chunk mismatch")
    if expected_chapter_id is not None and actual_chapter_id != expected_chapter_id:
        errors.append(f"{label}: evidence lies outside the reviewed chapter")
    digest = next(
        row["text_sha256"]
        for rows in context.chunks_by_chapter.values() for row in rows if row["chunk_id"] == chunk_id
    )
    supplied_hash = anchor.get("text_sha256", anchor.get("chunk_text_sha256"))
    start, end = anchor.get("start"), anchor.get("end")
    if supplied_hash != digest:
        errors.append(f"{label}: chunk hash mismatch")
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
        errors.append(f"{label}: invalid chunk character range")
        return False
    if text[start:end] != anchor.get("quote_exact"):
        errors.append(f"{label}: quote_exact mismatch")
    return True


def _load_observations(
    context: Context, artifact_root: Path, errors: list[str]
) -> dict[str, dict[str, Any]]:
    directory = artifact_root / "literary_observations"
    if not directory.exists():
        return {}
    observations: dict[str, dict[str, Any]] = {}
    from literary.tools import alignment_v2 as v2

    for path in v2.record_paths(directory):
        for index, row in enumerate(read_jsonl(path), 1):
            label = f"{path.name}:{index}"
            _check_record_hash(row, label, errors)
            _check_named_human_text_fields(row, label, errors)
            record_id = row.get("observation_id")
            if not isinstance(record_id, str) or not record_id or record_id in observations:
                errors.append(f"{label}: invalid or duplicate observation_id")
            else:
                observations[record_id] = row
            if row.get("schema_version", 1) not in {1, 2}:
                errors.append(f"{label}: unsupported observation schema_version")
            if row.get("schema_version") == 2:
                if not path.name.startswith("FA-V"):
                    errors.append(f"{label}: v2 observations require batch-local files")
                if row.get("record_type") != "literary_observation":
                    errors.append(f"{label}: invalid v2 observation record_type")
                if row.get("handoff_disposition") != "pending" or row.get("review_state") != "proposed":
                    errors.append(f"{label}: immutable v2 observations require proposed/pending; routing belongs to adjudication")
                if row.get("introduced_by_stage") not in STAGES:
                    errors.append(f"{label}: introduced_by_stage must identify a review stage")
                if not isinstance(row.get("producer_task_id"), str) or not row["producer_task_id"].strip():
                    errors.append(f"{label}: producer_task_id is required")
            if path.name.startswith("FA-V"):
                parts = path.stem.split(".")
                if context.chapter_to_batch.get(row.get("chapter_id")) != parts[0]:
                    errors.append(f"{label}: observation chapter does not match batch-local filename")
                if len(parts) > 1 and (len(parts) != 2 or parts[1] not in STAGES
                                      or row.get("introduced_by_stage") != parts[1]):
                    errors.append(f"{label}: observation stage does not match batch-local filename")
            if row.get("chapter_id") not in context.chapter_by_id:
                errors.append(f"{label}: unknown chapter_id")
            for field in (
                "candidate_label", "observation", "does_not_claim", "observation_type"
            ):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    errors.append(f"{label}: {field} must be a non-empty string")
            if row.get("observation_type") not in OBSERVATION_TYPES:
                errors.append(f"{label}: invalid observation_type")
            if "subkind" in row and (
                not isinstance(row["subkind"], str) or not row["subkind"].strip()
            ):
                errors.append(f"{label}: subkind must be non-empty free text when present")
            if not isinstance(row.get("review_state"), str) or not row["review_state"].strip():
                errors.append(f"{label}: review_state is required")
            if row.get("handoff_disposition") not in OBSERVATION_DISPOSITIONS:
                errors.append(f"{label}: invalid handoff_disposition")
            anchors = row.get("evidence_anchors")
            if not isinstance(anchors, list) or not anchors:
                errors.append(f"{label}: evidence_anchors must be a non-empty list")
            else:
                for anchor_index, anchor in enumerate(anchors, 1):
                    if not isinstance(anchor, dict):
                        errors.append(f"{label}: evidence anchor {anchor_index} is not an object")
                    else:
                        _validate_chunk_anchor(
                            context, anchor, f"{label}:anchor-{anchor_index}", errors,
                            row.get("chapter_id"),
                        )
            if "consumed_context_evidence_anchors" in row:
                context_anchors = row["consumed_context_evidence_anchors"]
                if any(isinstance(row.get(field), str) and row[field] in {"raw", "diplomatic", "source", "original"}
                       for field in ("reading_layer", "text_layer", "layer")):
                    errors.append(f"{label}: observation layer cannot disclaim consumed critical context")
                if row.get("schema_version") != 2 or not isinstance(context_anchors, list) or not context_anchors:
                    errors.append(f"{label}: consumed context anchors require v2 and a non-empty list")
                else:
                    for anchor_index, anchor in enumerate(context_anchors, 1):
                        if not isinstance(anchor, dict):
                            errors.append(f"{label}: consumed context anchor {anchor_index} is not an object")
                            continue
                        _validate_chunk_anchor(context, anchor, f"{label}:context-anchor-{anchor_index}", errors)
                        if isinstance(anchor.get("chunk_id"), str) and anchor["chunk_id"].split("_", 1)[0] == row.get("chapter_id"):
                            errors.append(f"{label}: same-chapter evidence belongs in evidence_anchors")
                        if any(isinstance(anchor.get(field), str) and anchor[field] in {"raw", "diplomatic", "source", "original"}
                               for field in ("reading_layer", "text_layer", "layer")):
                            errors.append(f"{label}: consumed context cannot disclaim critical reading dependency")
    return observations


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict)):
        return bool(value)
    return value is not None


def _require_list_fields(
    row: dict[str, Any], fields: Iterable[str], label: str, errors: list[str]
) -> None:
    for field in fields:
        if not isinstance(row.get(field), list):
            errors.append(f"{label}: {field} must be a list (it may be empty)")


def _supports_unresolvable(issue: dict[str, Any]) -> bool:
    adjudication = issue.get("adjudication")
    return (
        issue.get("_preflight_state") == "completed"
        and isinstance(adjudication, dict)
        and adjudication.get("disposition") == "unresolvable"
        and isinstance(adjudication.get("competing_resolutions"), list)
        and len(adjudication["competing_resolutions"]) >= 2
        and _has_content(adjudication.get("search_scope"))
        and _has_content(adjudication.get("rationale"))
    )


def _expected_stage_subrecord(
    context: Context,
    reading_hashes: dict[str, str],
    applied_ids: dict[str, list[str]],
    chapter_id: str,
) -> dict[str, Any]:
    row = context.chapter_by_id[chapter_id]
    return {
        "raw_chapter_sha256": row["_raw_sha256"],
        "reading_chapter_sha256": reading_hashes[chapter_id],
        "chunk_locks": [
            {"chunk_id": chunk["chunk_id"], "text_sha256": chunk["text_sha256"]}
            for chunk in context.chunks_by_chapter[chapter_id]
        ],
        "neighbor_locks": {
            "previous": _reading_neighbor_lock(context, reading_hashes, chapter_id, -1),
            "next": _reading_neighbor_lock(context, reading_hashes, chapter_id, 1),
        },
        "applied_patch_ids": applied_ids[chapter_id],
    }


def _load_stage(
    context: Context,
    artifact_root: Path,
    stage: str,
    reading_hashes: dict[str, str],
    applied_ids: dict[str, list[str]],
    issues: dict[str, dict[str, Any]],
    observations: dict[str, dict[str, Any]],
    patches: dict[str, dict[str, Any]],
    artifact_locks: dict[str, str],
    errors: list[str],
    checkpoint: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    from literary.tools import alignment_v2 as v2

    directory = artifact_root / stage
    records: dict[str, dict[str, Any]] = {}
    if not directory.exists():
        return records
    for path in v2.record_paths(directory):
        rows = read_jsonl(path)
        if not rows:
            errors.append(f"{stage}/{path.name}: empty stage files are not allowed")
        file_volumes: set[str] = set()
        for index, row in enumerate(rows, 1):
            label = f"{stage}/{path.name}:{index}"
            _check_record_hash(row, label, errors)
            batch_id = row.get("batch_id")
            plan = context.batch_by_id.get(batch_id)
            if plan is None:
                errors.append(f"{label}: unknown batch_id")
                continue
            if batch_id in records:
                errors.append(f"{label}: duplicate {stage} record for {batch_id}")
                continue
            records[batch_id] = row
            file_volumes.add(plan["volume_title"])
            if row.get("schema_version") not in {1, 2} or row.get("record_type") != "alignment_batch_review":
                errors.append(f"{label}: invalid stage record contract")
            if row.get("schema_version") == 2 and not path.name.startswith("FA-V"):
                errors.append(f"{label}: v2 stages require batch-local files")
            if path.name.startswith("FA-V") and path.stem != batch_id:
                errors.append(f"{label}: stage batch does not match batch-local filename")
            if row.get("stage") != stage:
                errors.append(f"{label}: stage does not match directory")
            if row.get("volume_title") != plan["volume_title"]:
                errors.append(f"{label}: volume_title mismatch")
            if row.get("batch_index_record_sha256") != plan["record_sha256"]:
                errors.append(f"{label}: stale batch plan lock")
            if row.get("subject_sha256") != plan["record_sha256"]:
                errors.append(f"{label}: subject_sha256 must lock the batch plan record")
            if row.get("input_manifest_sha256") != context.manifest_file_sha256:
                errors.append(
                    f"{label}: input_manifest_sha256 must use complete pinned manifest file bytes"
                )
            v2.validate_stage_inputs(
                row, v2.local_dependency_locks(context, batch_id, issues, patches, reading_hashes),
                artifact_locks, checkpoint, label, errors,
            )
            if not isinstance(row.get("task_id"), str) or not row["task_id"].strip():
                errors.append(f"{label}: task_id is required")
            subrecords = row.get("chapters")
            expected_ids = [lock["chapter_id"] for lock in plan["chapter_locks"]]
            if not isinstance(subrecords, list) or [item.get("chapter_id") if isinstance(item, dict) else None for item in subrecords] != expected_ids:
                errors.append(f"{label}: chapters must exactly cover the complete batch in order")
                continue
            for sub_index, subrecord in enumerate(subrecords, 1):
                chapter_id = subrecord["chapter_id"]
                sublabel = f"{label}:{chapter_id}"
                _check_named_human_text_fields(subrecord, sublabel, errors)
                expected = _expected_stage_subrecord(context, reading_hashes, applied_ids, chapter_id)
                for key, value in expected.items():
                    if subrecord.get(key) != value:
                        errors.append(f"{sublabel}: {key} mismatch")
                issue_ids = subrecord.get("issue_ids")
                if not isinstance(issue_ids, list) or len(issue_ids) != len(set(issue_ids)) or any(
                    issue_id not in issues
                    or issues[issue_id].get("chapter_id") != chapter_id
                    for issue_id in issue_ids
                ):
                    errors.append(
                        f"{sublabel}: issue_ids must be unique same-chapter references to issues.jsonl"
                    )
                expected_chunks = [chunk["chunk_id"] for chunk in context.chunks_by_chapter[chapter_id]]
                if stage in {"production", "counter_review"} and subrecord.get("read_chunk_ids") != expected_chunks:
                    errors.append(f"{sublabel}: read_chunk_ids must list every chapter chunk once and in order")
                if stage == "production":
                    for field in (
                        "narrative_digest", "continuity_in", "continuity_out", "evidence_refs",
                        "does_not_claim",
                    ):
                        if not _has_content(subrecord.get(field)):
                            errors.append(f"{sublabel}: production {field} must be non-empty")
                    _require_list_fields(subrecord, PRODUCTION_LIST_FIELDS, sublabel, errors)
                    evidence_refs = subrecord.get("evidence_refs")
                    if isinstance(evidence_refs, list):
                        for evidence_index, anchor in enumerate(evidence_refs, 1):
                            if not isinstance(anchor, dict):
                                errors.append(f"{sublabel}: evidence_refs[{evidence_index}] is not an object")
                            else:
                                _validate_chunk_anchor(
                                    context, anchor, f"{sublabel}:evidence-{evidence_index}",
                                    errors, chapter_id,
                                )
                elif stage == "counter_review":
                    if not _has_content(subrecord.get("recommendation")):
                        errors.append(f"{sublabel}: counter_review recommendation must be non-empty")
                    if not _has_content(subrecord.get("rationale")):
                        errors.append(f"{sublabel}: counter_review rationale must be non-empty")
                    _require_list_fields(subrecord, COUNTER_LIST_FIELDS, sublabel, errors)
                else:
                    final_status = subrecord.get("final_status")
                    if final_status not in ALL_ADJUDICATION_STATUSES:
                        errors.append(f"{sublabel}: invalid final_status")
                    if not _has_content(subrecord.get("rationale")):
                        errors.append(f"{sublabel}: adjudication rationale must be non-empty")
                    adjudication_lists = (
                        "accepted_observation_ids", "rejected_observation_ids", "issue_ids", "patch_ids"
                    )
                    _require_list_fields(subrecord, adjudication_lists, sublabel, errors)
                    accepted = subrecord.get("accepted_observation_ids", [])
                    rejected = subrecord.get("rejected_observation_ids", [])
                    patch_ids = subrecord.get("patch_ids", [])
                    if isinstance(accepted, list) and isinstance(rejected, list):
                        if len(accepted) != len(set(accepted)) or len(rejected) != len(set(rejected)):
                            errors.append(f"{sublabel}: observation ID lists must not contain duplicates")
                        if set(accepted) & set(rejected):
                            errors.append(f"{sublabel}: accepted and rejected observations must be disjoint")
                        if any(
                            observation_id not in observations
                            or observations[observation_id].get("chapter_id") != chapter_id
                            for observation_id in accepted + rejected
                        ):
                            errors.append(
                                f"{sublabel}: adjudication observations must exist in the same chapter"
                            )
                    if isinstance(patch_ids, list):
                        if len(patch_ids) != len(set(patch_ids)) or any(
                            patch_id not in patches or patches[patch_id].get("chapter_id") != chapter_id
                            for patch_id in patch_ids
                        ):
                            errors.append(f"{sublabel}: patch_ids must be unique same-chapter approved patches")
                    if final_status == "unresolvable":
                        linked = (
                            [issues[issue_id] for issue_id in issue_ids if issue_id in issues]
                            if isinstance(issue_ids, list)
                            else []
                        )
                        if not linked or not any(_supports_unresolvable(issue) for issue in linked):
                            errors.append(
                                f"{sublabel}: unresolvable requires a reviewed issue with at least "
                                "two competing resolutions, search_scope and rationale"
                            )
        if len(file_volumes) > 1:
            errors.append(f"{stage}/{path.name}: one stage file may not mix volumes")
    return records


def _validate_terminal_ledger(
    context: Context,
    artifact_root: Path,
    stages: dict[str, dict[str, dict[str, Any]]],
    reading_hashes: dict[str, str],
    errors: list[str],
) -> bool:
    path = artifact_root / "chapter_alignment.tsv"
    if not path.exists():
        return False
    required = {
        "chapter_id", "status", "raw_chapter_sha256", "reading_chapter_sha256",
        "production_task_id", "counter_review_task_id", "adjudication_task_id",
        "batch_id", "adjudication_record_sha256",
    }
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not required.issubset(set(reader.fieldnames or [])):
            errors.append("chapter_alignment.tsv: missing required columns")
            return True
        rows = list(reader)
    if len(rows) != len(context.chapters):
        errors.append("chapter_alignment.tsv: terminal ledger must contain exactly 1439 rows")
    seen: set[str] = set()
    for row in rows:
        chapter_id = row["chapter_id"]
        if chapter_id in seen or chapter_id not in context.chapter_by_id:
            errors.append(f"chapter_alignment.tsv: invalid or duplicate chapter {chapter_id}")
            continue
        seen.add(chapter_id)
        batch_id = context.chapter_to_batch[chapter_id]
        if row["batch_id"] != batch_id:
            errors.append(f"chapter_alignment.tsv: batch_id mismatch for {chapter_id}")
        try:
            production = stages["production"][batch_id]
            counter = stages["counter_review"][batch_id]
            adjudication = stages["adjudication"][batch_id]
        except KeyError:
            errors.append(f"chapter_alignment.tsv: missing stage chain for {chapter_id}")
            continue
        if row["adjudication_record_sha256"] != adjudication.get("record_sha256"):
            errors.append(
                f"chapter_alignment.tsv: adjudication_record_sha256 mismatch for {chapter_id}"
            )
        chapter_records = adjudication.get("chapters")
        adjudicated = next(
            (
                item for item in chapter_records
                if isinstance(item, dict) and item.get("chapter_id") == chapter_id
            ),
            None,
        ) if isinstance(chapter_records, list) else None
        if adjudicated is None:
            errors.append(
                f"chapter_alignment.tsv: adjudication has no chapter subrecord for {chapter_id}"
            )
            continue
        if row["status"] != adjudicated.get("final_status") or row["status"] not in TERMINAL_STATUSES:
            errors.append(f"chapter_alignment.tsv: status mismatch for {chapter_id}")
        expected = context.chapter_by_id[chapter_id]
        if row["raw_chapter_sha256"] != expected["_raw_sha256"] or row["reading_chapter_sha256"] != reading_hashes[chapter_id]:
            errors.append(f"chapter_alignment.tsv: chapter hash mismatch for {chapter_id}")
        task_ids = [
            production.get("task_id"), counter.get("task_id"), adjudication.get("task_id")
        ]
        if [row["production_task_id"], row["counter_review_task_id"], row["adjudication_task_id"]] != task_ids:
            errors.append(f"chapter_alignment.tsv: task identity mismatch for {chapter_id}")
    return True


def validate_alignment(
    context: Context, artifact_root: Path | None = None, *, final: bool = False
) -> tuple[dict[str, Any], list[str]]:
    from literary.tools import alignment_v2 as v2

    artifact_root = (artifact_root or context.alignment_root).resolve()
    errors: list[str] = []
    batch_path = artifact_root / "batch_index.jsonl"
    batch_index_present = batch_path.exists()
    if batch_index_present:
        try:
            expected_bytes = batch_index_bytes(context)
            if batch_path.read_bytes() != expected_bytes:
                errors.append("batch_index.jsonl is not the canonical deterministic 148-batch plan")
            expected_digest = context.manifest["batching"].get("batch_index_sha256")
            if expected_digest and hashlib.sha256(expected_bytes).hexdigest() != expected_digest:
                errors.append("generated batch index does not match manifest batch_index_sha256")
        except AlignmentError as exc:
            errors.append(str(exc))

    try:
        issues = _load_issues(context, artifact_root, errors)
        patches_by_chapter, reading_hashes, applied_ids, projection = _load_patches(
            context, artifact_root, issues, errors
        )
        patches = {
            row["patch_id"]: row
            for rows in patches_by_chapter.values()
            for row in rows
            if isinstance(row.get("patch_id"), str)
        }
        observations = _load_observations(context, artifact_root, errors)
        projection_digest = hashlib.sha256(projection).hexdigest()
        projection_path = reading_projection_path(context, artifact_root)
        projection_present = projection_path.exists()
        if projection_present and projection_path.read_bytes() != projection:
            errors.append(
                "reading projection snapshot does not equal deterministic raw source plus approved patches"
            )
        artifact_locks = {
            "source_sha256": context.manifest["source"]["sha256"],
            "issues_sha256": _file_sha256_or_empty(artifact_root / "issues.jsonl"),
            "patches_sha256": _file_sha256_or_empty(artifact_root / "patches.jsonl"),
            "reading_projection_sha256": projection_digest,
        }
        checkpoint = v2.load_checkpoint(context, artifact_root, errors)
        stages = {
            stage: _load_stage(
                context, artifact_root, stage, reading_hashes, applied_ids, issues,
                observations, patches, artifact_locks, errors, checkpoint
            )
            for stage in STAGES
        }
        v2.validate_checkpoint_inventory(checkpoint, stages, observations, errors)
        v2_routed = v2.validate_observation_references(stages, observations, context, errors, final=final)
    except (AlignmentError, OSError) as exc:
        errors.append(str(exc))
        issues = {}
        patches = {}
        observations = {}
        reading_hashes = {chapter_id: row["_raw_sha256"] for chapter_id, row in context.chapter_by_id.items()}
        projection_digest = context.manifest["source"]["sha256"]
        projection_present = False
        stages = {stage: {} for stage in STAGES}
        v2_routed = set()

    if any(stages[stage] for stage in STAGES) and not batch_index_present:
        errors.append("stage records exist but batch_index.jsonl is absent; run plan first")
    if any(stages[stage] for stage in STAGES) and not projection_present:
        errors.append("stage records exist but the deterministic reading projection snapshot is absent")

    for batch_id in context.batch_by_id:
        production = stages["production"].get(batch_id)
        counter = stages["counter_review"].get(batch_id)
        adjudication = stages["adjudication"].get(batch_id)
        if counter:
            if not production:
                errors.append(f"{batch_id}: counter_review has no production upstream")
            elif counter.get("production_record_sha256") != production.get("record_sha256"):
                errors.append(f"{batch_id}: counter_review production hash mismatch")
        if adjudication:
            if not production or not counter:
                errors.append(f"{batch_id}: adjudication chain is incomplete")
            else:
                if adjudication.get("production_record_sha256") != production.get("record_sha256"):
                    errors.append(f"{batch_id}: adjudication production hash mismatch")
                if adjudication.get("counter_record_sha256") != counter.get("record_sha256"):
                    errors.append(f"{batch_id}: adjudication counter-review hash mismatch")
        task_ids = [row.get("task_id") for row in (production, counter, adjudication) if row]
        if len(task_ids) != len(set(task_ids)):
            errors.append(f"{batch_id}: production, counter_review and adjudication task_id values must be distinct")

    terminal = 0
    repair_required = 0
    for row in stages["adjudication"].values():
        for chapter in row.get("chapters", []):
            status = chapter.get("final_status")
            terminal += status in TERMINAL_STATUSES
            repair_required += status == "repair_required"

    ledger_present = _validate_terminal_ledger(
        context, artifact_root, stages, reading_hashes, errors
    )
    counts = {stage: len(stages[stage]) for stage in STAGES}
    report = {
        "pass": not errors,
        "final": final,
        "batch_index_present": batch_index_present,
        "batch_count": len(context.batch_index),
        "stage_batch_counts": counts,
        "terminal_chapters": terminal,
        "total_chapters": len(context.chapters),
        "repair_required": repair_required,
        "terminal_ledger_present": ledger_present,
        "reading_projection_present": projection_present,
        "reading_projection_sha256": projection_digest,
        "hash_rule": HASH_RULE,
        "chapter_hash_scope": CHAPTER_HASH_SCOPE,
    }
    if final:
        if not batch_index_present:
            errors.append("final-check requires batch_index.jsonl")
        if not ledger_present:
            errors.append("final-check requires chapter_alignment.tsv")
        if not projection_present:
            errors.append("final-check requires the deterministic reading projection snapshot")
        for stage in STAGES:
            if counts[stage] != len(context.batch_index):
                errors.append(f"final-check requires 148 complete {stage} batch records")
        if terminal != len(context.chapters):
            errors.append(f"final-check requires all manifest chapters terminal, found {terminal}")
        if repair_required:
            errors.append(f"final-check requires 0 repair_required chapters, found {repair_required}")
        pending_issues = sorted(
            issue_id
            for issue_id, issue in issues.items()
            if issue.get("_preflight_state") != "completed"
        )
        if pending_issues:
            errors.append(
                "final-check rejects pending issues: " + ", ".join(pending_issues)
            )
        pending_observations = sorted(
            observation_id
            for observation_id, observation in observations.items()
            if observation.get("handoff_disposition") == "pending"
            and observation_id not in v2_routed
        )
        if pending_observations:
            errors.append(
                "final-check rejects pending observations: "
                + ", ".join(pending_observations)
            )
        issue_references: set[str] = set()
        patch_references: set[str] = set()
        observation_references: set[str] = set()
        for batch in stages["adjudication"].values():
            for chapter in batch.get("chapters", []):
                if not isinstance(chapter, dict):
                    continue
                issue_references.update(
                    item for item in chapter.get("issue_ids", []) if isinstance(item, str)
                )
                patch_references.update(
                    item for item in chapter.get("patch_ids", []) if isinstance(item, str)
                )
                for field in ("accepted_observation_ids", "rejected_observation_ids"):
                    observation_references.update(
                        item for item in chapter.get(field, []) if isinstance(item, str)
                    )
        orphan_issues = sorted(set(issues) - issue_references)
        orphan_patches = sorted(set(patches) - patch_references)
        orphan_observations = sorted(set(observations) - observation_references)
        if orphan_issues:
            errors.append("final-check rejects orphan issues: " + ", ".join(orphan_issues))
        if orphan_patches:
            errors.append("final-check rejects orphan patches: " + ", ".join(orphan_patches))
        if orphan_observations:
            errors.append(
                "final-check rejects orphan observations: " + ", ".join(orphan_observations)
            )
        report["pass"] = not errors
    return report, errors


def _print_plan(context: Context) -> None:
    maximum = max(row["chapter_body_char_count"] for row in context.batch_index)
    print(f"{len(context.batch_index)} batches | {len(context.chapters)} chapters | max {maximum} body chars")
    for row in context.batch_index:
        print(
            f"{row['batch_id']}  {row['volume_title']}  "
            f"{row['first_chapter_id']}–{row['last_chapter_id']}  "
            f"{row['chapter_count']:>2} chapters  {row['chapter_body_char_count']:>5} chars"
        )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "check", "final-check", "checkpoint", "locks"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--batch-id", help="batch ID for the mechanical locks command")
    parser.add_argument(
        "--no-write", action="store_true",
        help="print plan without updating batch_index.jsonl or the reading projection",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        context = load_context(args.root)
        from literary.tools import alignment_selected
        is_managed = alignment_selected.managed(context.alignment_root)
        if is_managed and (args.command in {"checkpoint", "locks"} or (args.command == "plan" and not args.no_write)):
            raise AlignmentError("managed release selected: frozen history is read-only; use release candidate locks/check commands")
        if args.command in {"checkpoint", "locks"}:
            from literary.tools import alignment_v2 as v2

            if args.command == "checkpoint":
                print(f"mechanical compatibility checkpoint (not literary review): {v2.checkpoint_legacy(context)}")
            else:
                if not args.batch_id:
                    raise AlignmentError("locks requires --batch-id")
                print(json.dumps(v2.stage_input_locks(context, args.batch_id), ensure_ascii=False, indent=2))
            return 0
        if args.command == "plan":
            if not args.no_write:
                path = write_batch_index(context)
                print(f"wrote {path}")
                projection_path = generate_reading_projection(context)
                print(f"wrote {projection_path}")
            print(f"batch_index SHA-256: {hashlib.sha256(batch_index_bytes(context)).hexdigest()}")
            _print_plan(context)
            return 0
        if is_managed:
            report, errors = alignment_selected.check(context, context.alignment_root, final=args.command == "final-check")
        else:
            report, errors = validate_alignment(context, final=args.command == "final-check")
    except (AlignmentError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    status = "PASS" if report["pass"] else "FAIL"
    print(
        f"{status} mechanical contract (not literary understanding): "
        f"alignment {args.command}"
    )
    print(f"batches: {report['batch_count']} (index: {'present' if report['batch_index_present'] else 'absent; run plan'})")
    print(
        "stage batches: "
        + ", ".join(f"{stage}={report['stage_batch_counts'][stage]}/manifest count" for stage in STAGES)
    )
    print(
        f"terminal chapters: {report['terminal_chapters']}/{report['total_chapters']} | "
        f"repair_required: {report['repair_required']}"
    )
    if report.get("selection_and_holds"):
        print(f"review holds: {len(report['selection_and_holds']['holds'])} | held batches: {len(report['selection_and_holds']['held_batch_ids'])}")
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Preserve the historic direct-script CLI as well as python -m invocation.
    sys.path.insert(0, str(ROOT))
    sys.modules.setdefault("literary.tools.alignment", sys.modules[__name__])
    raise SystemExit(main())
