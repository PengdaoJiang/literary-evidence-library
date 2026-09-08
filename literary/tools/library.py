#!/usr/bin/env python3
"""Thin compiler and read-only query CLI for the literary curation layer.

The program never decides literary facts.  It only validates reviewed literary
records, compiles them into SQLite, and exposes fact-only queries.  Pending
records live in a separate table and are reachable only through the explicit
``candidates`` command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
LITERARY_ROOT = ROOT / "literary"
CURATION_ROOT = LITERARY_ROOT / "curation"
SCHEMA_PATH = LITERARY_ROOT / "schema" / "knowledge.sql"
DEFAULT_DB = LITERARY_ROOT / "generated" / "knowledge.sqlite"
DEFAULT_MANIFEST = LITERARY_ROOT / "generated" / "manifest.json"
REPORT_ROOT = LITERARY_ROOT / "generated" / "reports"


class CurationError(RuntimeError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise CurationError(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise CurationError(f"non-finite JSON number: {token}")
    return value


def strict_loads(text: str, source: Path) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=lambda value: (_ for _ in ()).throw(
                CurationError(f"non-finite JSON constant in {source}: {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CurationError(f"invalid JSON in {source}: {exc}") from exc


def load_json(path: Path) -> Any:
    return strict_loads(path.read_text(encoding="utf-8"), path)


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = strict_loads(line, path)
            if not isinstance(value, dict):
                raise CurationError(f"JSONL row is not an object: {path}:{line_number}")
            yield line_number, value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    digest = sha256_bytes(canonical_json(list(parts)).encode("utf-8"))[:24]
    return f"{prefix}.{digest}"


def expected_mention_id(
    entity_id: str, chunk_id: str, start: int, end: int, surface: str
) -> str:
    """Reproduce the frozen mention-ledger ID without importing its old pipeline."""
    token = "\x1f".join(
        canonical_json(part) for part in (entity_id, chunk_id, start, end, surface)
    )
    return f"mnt.{sha256_bytes(token.encode('utf-8'))[:24]}"


def expected_occurrence_id(row: dict[str, Any]) -> str:
    anchor = row["evidence_anchor"]
    return stable_id(
        "roc",
        row["subject_entity_id"],
        row["predicate"],
        row["object_entity_id"],
        row["qualifiers"],
        row["polarity"],
        row["modality"],
        row["perspective"],
        row["certainty"],
        row["assertion_scope"],
        anchor["chunk_id"],
        anchor["start"],
        anchor["end"],
        anchor["quote_exact"],
    )


def expected_episode_id(row: dict[str, Any]) -> str:
    return stable_id(
        "rep",
        row["relation_key"],
        row["story_time"],
        row["state_at_close"],
        row["support_occurrence_refs"],
        row["challenge_occurrence_refs"],
    )


def expected_endpoint_resolution_id(row: dict[str, Any]) -> str:
    previous = row["previous_binding"]
    resolved = row["resolved_binding"]
    anchor = row["evidence_anchor"]
    return stable_id(
        "ers",
        previous.get("mention_id"),
        resolved["mention_id"],
        previous.get("entity_id"),
        resolved["entity_id"],
        anchor["chunk_id"],
        anchor["start"],
        anchor["end"],
        anchor["quote_exact"],
    )


def repo_path(relative: str) -> Path:
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise CurationError(f"unsafe repository path: {relative}")
    resolved = (ROOT / requested).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise CurationError(f"path leaves repository: {relative}") from exc
    if not resolved.is_file():
        raise CurationError(f"missing input file: {relative}")
    return resolved


def relpath(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise CurationError(f"missing {key} in {where}")
    return mapping[key]


def _discover_jsonl(
    directory: Path, pattern: str, label: str, *, required: bool
) -> tuple[Path, ...]:
    if not directory.is_dir():
        raise CurationError(f"missing {label} directory: {relpath(directory)}")
    paths = tuple(sorted(directory.glob(pattern), key=lambda path: path.name.casefold()))
    if required and not paths:
        raise CurationError(f"no {label} files discovered: {relpath(directory)}/{pattern}")
    if any(not path.is_file() for path in paths):
        raise CurationError(f"non-file entry in {label} discovery")
    if len({path.resolve() for path in paths}) != len(paths):
        raise CurationError(f"duplicate path in {label} discovery")
    return paths


def _discover_batches(
    directory: Path, prefix: str, label: str, *, required: bool
) -> dict[str, Path]:
    paths = _discover_jsonl(directory, f"{prefix}*.jsonl", label, required=required)
    discovered: dict[str, Path] = {}
    for path in paths:
        token = path.stem.removeprefix(prefix)
        if not token or any(not (char.isalnum() or char in "_-") for char in token):
            raise CurationError(f"invalid {label} filename: {relpath(path)}")
        batch_id = token.upper()
        if batch_id in discovered:
            raise CurationError(f"duplicate {label} batch: {batch_id}")
        discovered[batch_id] = path
    return discovered


def _discover_rounds(
    directory: Path, prefix: str, label: str, *, required: bool
) -> dict[int, Path]:
    paths = _discover_jsonl(directory, f"{prefix}*.jsonl", label, required=required)
    discovered: dict[int, Path] = {}
    for path in paths:
        token = path.stem.removeprefix(prefix)
        if not token.isdigit() or int(token) < 1:
            raise CurationError(f"invalid {label} filename: {relpath(path)}")
        round_number = int(token)
        if round_number in discovered:
            raise CurationError(f"duplicate {label} round: {round_number}")
        discovered[round_number] = path
    if discovered and set(discovered) != set(range(1, max(discovered) + 1)):
        raise CurationError(f"non-contiguous {label} rounds")
    return dict(sorted(discovered.items()))


def _discover_relation_inputs(relations_root: Path | None = None) -> dict[str, Any]:
    root = relations_root or CURATION_ROOT / "relations"
    reviews_root = root / "reviews"
    accepted = _discover_jsonl(root, "accepted*.jsonl", "accepted relation", required=True)
    pending_batches = _discover_batches(
        root / "pending", "batch_", "pending relation", required=True
    )
    review_batches = _discover_batches(
        reviews_root, "batch_", "relation review", required=True
    )
    if set(pending_batches) != set(review_batches):
        raise CurationError(
            "pending/review batch files do not exactly correspond: "
            f"pending={sorted(pending_batches)}, reviews={sorted(review_batches)}"
        )

    refinement_reviews = _discover_rounds(
        reviews_root, "refinements_round_", "refinement review", required=False
    )
    refinement_accepted = _discover_rounds(
        root, "accepted_refinements_round_", "accepted refinement", required=False
    )
    if set(refinement_reviews) != set(refinement_accepted):
        raise CurationError("refinement review/accepted rounds do not exactly correspond")

    identity_reviews = _discover_rounds(
        reviews_root,
        "identity_refinements_round_",
        "identity refinement review",
        required=False,
    )
    identity_accepted = _discover_rounds(
        root,
        "accepted_identity_refinements_round_",
        "accepted identity refinement",
        required=False,
    )
    if set(identity_reviews) != set(identity_accepted):
        raise CurationError(
            "identity refinement review/accepted rounds do not exactly correspond"
        )

    gold = _discover_rounds(root, "gold_round_", "relation gold", required=True)
    promotion_specs = reviews_root / "promotion_specs.jsonl"
    if not promotion_specs.is_file():
        raise CurationError(f"missing promotion specs: {relpath(promotion_specs)}")

    return {
        "accepted": accepted,
        "pending_batches": pending_batches,
        "review_batches": review_batches,
        "refinement_reviews": refinement_reviews,
        "refinement_accepted": refinement_accepted,
        "identity_reviews": identity_reviews,
        "identity_accepted": identity_accepted,
        "gold": gold,
        "promotion_specs": promotion_specs,
    }


def dataset_profile() -> dict[str, Any]:
    """Explicit dataset contract replacing a single novel's fixed inventory."""
    profile = load_json(ROOT / "dataset.json")
    required = {"chapters", "chunks", "naming_decisions"}
    counts = profile.get("expected_counts", {})
    if set(counts) != required or any(type(counts[k]) is not int or counts[k] < 1 for k in required):
        raise CurationError("dataset counts must be positive integers for all required inventories")
    repo_path(profile["source_text"])
    if not isinstance(profile.get("focus_entity"), str) or not profile["focus_entity"]:
        raise CurationError("dataset focus_entity is required")
    return profile


def _input_inventory(
    chunk_paths: list[Path],
    summary_paths: list[Path],
    relation_inputs: dict[str, Any],
) -> list[tuple[Path, str, int | None]]:
    fixed = [
        (SCHEMA_PATH, "schema", None),
        (Path(__file__).resolve(), "compiler", None),
        (ROOT / "source" / "manifest" / "source_meta.json", "source_manifest", None),
        (ROOT / "source" / "manifest" / "source.sha256", "source_manifest", None),
        (repo_path(dataset_profile()["source_text"]), "source_text", None),
        (ROOT / "dataset.json", "dataset_contract", None),
        (ROOT / "build" / "indices" / "chapters.jsonl", "chapter_index", dataset_profile()["expected_counts"]["chapters"]),
        (ROOT / "build" / "indices" / "chunks.jsonl", "chunk_index", dataset_profile()["expected_counts"]["chunks"]),
        (CURATION_ROOT / "entities" / "characters.jsonl", "literary_curation", None),
        (CURATION_ROOT / "entities" / "factions.jsonl", "literary_curation", None),
        (CURATION_ROOT / "entities" / "items.jsonl", "literary_curation", None),
        (CURATION_ROOT / "entities" / "locations.jsonl", "literary_curation", None),
        (CURATION_ROOT / "entities" / "extensions.jsonl", "literary_curation", None),
        (CURATION_ROOT / "entities" / "IDENTITY_DECISIONS.md", "literary_curation", None),
        (CURATION_ROOT / "naming" / "policy.json", "human_policy", None),
        (CURATION_ROOT / "naming" / "decisions.jsonl", "human_curation", dataset_profile()["expected_counts"]["naming_decisions"]),
        (CURATION_ROOT / "naming" / "pending.jsonl", "human_review_queue", None),
        (CURATION_ROOT / "mentions" / "alias_policy.json", "literary_policy", None),
        (CURATION_ROOT / "mentions" / "endpoint_resolutions.jsonl", "independent_literary_review", None),
    ]
    dynamic = [(path, "chunk_text", None) for path in chunk_paths]
    dynamic.extend((path, "chapter_summary", None) for path in summary_paths)
    dynamic.extend((path, "curated_relation_facts", None) for path in relation_inputs["accepted"])
    dynamic.extend(
        (path, "relation_review_queue", None)
        for path in relation_inputs["pending_batches"].values()
    )
    dynamic.extend(
        (path, "independent_literary_review", None)
        for path in relation_inputs["review_batches"].values()
    )
    dynamic.extend(
        (path, "independent_literary_review", None)
        for path in relation_inputs["refinement_reviews"].values()
    )
    dynamic.extend(
        (path, "independent_literary_review", None)
        for path in relation_inputs["identity_reviews"].values()
    )
    dynamic.append((relation_inputs["promotion_specs"], "curated_relation_plan", None))
    dynamic.extend(
        (path, "independent_literary_gold", None)
        for path in relation_inputs["gold"].values()
    )
    rows = fixed + dynamic
    if len({path.resolve() for path, _, _ in rows}) != len(rows):
        raise CurationError("duplicate input path in compiler inventory")
    missing = [path for path, _, _ in rows if not path.is_file()]
    if missing:
        raise CurationError("missing inputs: " + ", ".join(relpath(path) for path in missing))
    return sorted(rows, key=lambda row: relpath(row[0]))


def _verify_anchor(anchor: dict[str, Any], chunks: dict[str, dict[str, Any]], where: str) -> None:
    chunk_id = _require(anchor, "chunk_id", where)
    if chunk_id not in chunks:
        raise CurationError(f"unknown chunk in {where}: {chunk_id}")
    chunk = chunks[chunk_id]
    if anchor.get("chapter_id") != chunk["chapter_id"]:
        raise CurationError(f"chapter/chunk mismatch in {where}")
    if anchor.get("chunk_text_sha256") != chunk["text_sha256"]:
        raise CurationError(f"stale chunk hash in {where}")
    if anchor.get("path") and anchor["path"] != chunk["path"]:
        raise CurationError(f"stale chunk path in {where}")
    start = anchor.get("start")
    end = anchor.get("end")
    quote = anchor.get("quote_exact")
    if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(chunk["text"])):
        raise CurationError(f"invalid evidence offsets in {where}")
    if chunk["text"][start:end] != quote:
        raise CurationError(f"evidence quote does not match original text in {where}")


def _insert_qualifiers(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    record_id: str,
    qualifiers: list[dict[str, Any]],
) -> None:
    for qualifier in sorted(qualifiers, key=lambda item: (item["key"], item["value"])):
        connection.execute(
            f"INSERT INTO {table} ({id_column}, qualifier_key, qualifier_value) VALUES (?, ?, ?)",
            (record_id, qualifier["key"], qualifier["value"]),
        )


def graph_coverage(connection: sqlite3.Connection) -> dict[str, Any]:
    entity_types = {
        row["entity_id"]: row["entity_type"]
        for row in connection.execute("SELECT entity_id,entity_type FROM entities")
    }
    edges = {
        (row["relation_key"], row["subject_entity_id"], row["object_entity_id"])
        for row in connection.execute(
            "SELECT relation_key,subject_entity_id,object_entity_id FROM accepted_relations"
        )
    }
    occurrence_endpoints = {
        entity_id
        for row in connection.execute(
            "SELECT subject_entity_id,object_entity_id FROM relation_occurrences"
        )
        for entity_id in row
    }
    direct_only_count = connection.execute(
        "SELECT count(*) FROM relation_occurrences o WHERE NOT EXISTS "
        "(SELECT 1 FROM relation_episode_support s WHERE s.occurrence_id=o.occurrence_id)"
    ).fetchone()[0]
    adjacency = {entity_id: set() for entity_id in entity_types}
    for _, subject, object_ in edges:
        adjacency[subject].add(object_)
        adjacency[object_].add(subject)
    covered = {entity_id for entity_id, neighbors in adjacency.items() if neighbors}
    unseen = set(entity_types)
    component_sizes: list[int] = []
    while unseen:
        seed = unseen.pop()
        component = {seed}
        stack = [seed]
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        component_sizes.append(len(component))
    type_coverage: dict[str, dict[str, int]] = {}
    for entity_type in sorted(set(entity_types.values())):
        total = sum(value == entity_type for value in entity_types.values())
        type_coverage[entity_type] = {
            "covered": sum(entity_types[entity_id] == entity_type for entity_id in covered),
            "total": total,
        }
    return {
        "accepted_relation_keys": len(edges),
        "accepted_occurrences": connection.execute(
            "SELECT count(*) FROM relation_occurrences"
        ).fetchone()[0],
        "direct_only_occurrences": direct_only_count,
        "evidence_covered_entities": len(occurrence_endpoints),
        "covered_entities": len(covered),
        "total_entities": len(entity_types),
        "zero_degree_entities": len(entity_types) - len(covered),
        "degree_one_entities": sum(len(neighbors) == 1 for neighbors in adjacency.values()),
        "largest_component_entities": max(component_sizes, default=0),
        "component_count": len(component_sizes),
        "focus_relation_keys": sum(
            dataset_profile()["focus_entity"] in (subject, object_) for _, subject, object_ in edges
        ),
        "type_coverage": type_coverage,
    }


def build_database(output: Path = DEFAULT_DB) -> dict[str, Any]:
    output = output.resolve()
    allowed = (LITERARY_ROOT / "generated").resolve()
    try:
        output.relative_to(allowed)
    except ValueError as exc:
        raise CurationError("database output must stay under literary/generated") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        temporary.unlink()

    chapter_rows = [row for _, row in iter_jsonl(ROOT / "build" / "indices" / "chapters.jsonl")]
    chunk_index_rows = [row for _, row in iter_jsonl(ROOT / "build" / "indices" / "chunks.jsonl")]
    profile = dataset_profile()
    expected = profile["expected_counts"]
    if len(chapter_rows) != expected["chapters"] or len(chunk_index_rows) != expected["chunks"]:
        raise CurationError("chapter/chunk inventory differs from dataset contract")

    chunk_rows: list[dict[str, Any]] = []
    chunk_paths: list[Path] = []
    chunks: dict[str, dict[str, Any]] = {}
    for index_row in chunk_index_rows:
        path = repo_path(index_row["path"])
        payload = load_json(path)
        text = payload["text"]
        digest = sha256_bytes(text.encode("utf-8"))
        if digest != payload["text_sha256"] or digest != index_row["text_sha256"]:
            raise CurationError(f"chunk hash mismatch: {index_row['chunk_id']}")
        row = {
            "chunk_id": index_row["chunk_id"],
            "chapter_id": index_row["chapter_id"],
            "chunk_index": index_row["chunk_index"],
            "path": index_row["path"],
            "text_sha256": digest,
            "text": text,
        }
        if row["chunk_id"] in chunks:
            raise CurationError(f"duplicate chunk id: {row['chunk_id']}")
        chunks[row["chunk_id"]] = row
        chunk_rows.append(row)
        chunk_paths.append(path)

    summary_paths = sorted((ROOT / "knowledge" / "summaries" / "chapters").glob("*.jsonl"))
    relation_inputs = _discover_relation_inputs()
    input_inventory = _input_inventory(chunk_paths, summary_paths, relation_inputs)

    connection = sqlite3.connect(temporary)
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.execute("BEGIN")

        for row in sorted(chapter_rows, key=lambda item: item["chapter_number"]):
            connection.execute(
                "INSERT INTO chapters VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row["chapter_id"], row["chapter_number"], row["title"], row.get("volume_title"),
                    row["path"], row["char_count"], row["paragraph_count"],
                ),
            )

        for row in sorted(chunk_rows, key=lambda item: (item["chapter_id"], item["chunk_index"])):
            connection.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?)",
                (row["chunk_id"], row["chapter_id"], row["chunk_index"], row["path"], row["text_sha256"], row["text"]),
            )

        summary_count = 0
        for path in summary_paths:
            for _, row in iter_jsonl(path):
                connection.execute(
                    "INSERT INTO chapter_summaries VALUES (?, ?, ?, ?, ?)",
                    (
                        row["chapter_id"], row["summary"], canonical_json(row.get("key_points", [])),
                        canonical_json(row.get("evidence_refs", [])), relpath(path),
                    ),
                )
                summary_count += 1
        if summary_count != expected["chapters"]:
            raise CurationError(f"summary inventory differs from dataset contract: {summary_count}")

        naming_path = CURATION_ROOT / "naming" / "decisions.jsonl"
        naming_rows = [row for _, row in iter_jsonl(naming_path)]
        naming = {row["entity_id"]: row for row in naming_rows}
        if len(naming) != len(naming_rows) or len(naming) != expected["naming_decisions"]:
            raise CurationError("naming decision ledger differs from dataset contract")

        entity_paths = sorted((CURATION_ROOT / "entities").glob("*.jsonl"))
        entity_rows = [row for path in entity_paths for _, row in iter_jsonl(path)]
        entity_ids = {row["entity_id"] for row in entity_rows}
        if len(entity_ids) != len(entity_rows):
            raise CurationError("entity ledger contains duplicate IDs")
        if not set(naming).issubset(entity_ids):
            raise CurationError("naming ledger references an unknown entity")

        for row in sorted(entity_rows, key=lambda item: item["entity_id"]):
            decision = naming.get(row["entity_id"])
            english = decision["canonical_english_name"] if decision else None
            naming_status = decision["decision_status"] if decision else "pending_naming_governance"
            connection.execute(
                "INSERT INTO entities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["entity_id"], row["entity_type"], row["name"], english,
                    naming_status, row["summary"], row.get("first_seen_chapter_id"),
                    row.get("last_seen_chapter_id"), canonical_json(row),
                ),
            )
            aliases = sorted(set(row.get("aliases", [])))
            for alias in aliases:
                connection.execute(
                    "INSERT INTO entity_aliases VALUES (?, ?, 'source_alias')",
                    (row["entity_id"], alias),
                )
            if english:
                connection.execute(
                    "INSERT OR IGNORE INTO entity_aliases VALUES (?, ?, 'english_name')",
                    (row["entity_id"], english),
                )
            for tag in sorted(set(row.get("tags", []))):
                connection.execute("INSERT INTO entity_tags VALUES (?, ?)", (row["entity_id"], tag))
            for ref in sorted(row.get("evidence_refs", []), key=lambda item: (item["chapter_id"], item["chunk_id"])):
                connection.execute(
                    "INSERT OR IGNORE INTO entity_evidence VALUES (?, ?, ?)",
                    (row["entity_id"], ref["chapter_id"], ref["chunk_id"]),
                )
            if decision:
                connection.execute(
                    "INSERT INTO naming_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["entity_id"], english, decision["decision_status"], decision["decision_batch_id"],
                        decision.get("decision_note"), decision.get("policy_version"),
                        decision.get("lifecycle_status"), canonical_json(decision),
                    ),
                )

        for _, row in iter_jsonl(CURATION_ROOT / "naming" / "pending.jsonl"):
            connection.execute(
                "INSERT INTO naming_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["candidate_id"], row["current_entity_id"], row["current_english_name"],
                    row["proposed_entity_id"], row["proposed_english_name"], row["status"],
                    row["reason"], canonical_json(row),
                ),
            )

        resolution_path = CURATION_ROOT / "mentions" / "endpoint_resolutions.jsonl"
        resolution_rows = [row for _, row in iter_jsonl(resolution_path)]
        resolutions: dict[str, dict[str, Any]] = {}
        resolved_mention_ids: set[str] = set()
        for row in resolution_rows:
            resolution_id = row.get("resolution_id")
            if (
                row.get("record_type") != "endpoint_resolution"
                or resolution_id != expected_endpoint_resolution_id(row)
                or resolution_id in resolutions
            ):
                raise CurationError(f"invalid or duplicate endpoint resolution: {resolution_id}")
            previous = row["previous_binding"]
            resolved = row["resolved_binding"]
            anchor = row["evidence_anchor"]
            lifecycle = row["lifecycle"]
            review = lifecycle["review"]
            if (
                resolved.get("entity_id") not in entity_ids
                or previous.get("entity_id") not in entity_ids | {None}
                or not str(resolved.get("mention_id", "")).startswith("mnt.")
                or resolved.get("mention_id")
                != expected_mention_id(
                    resolved["entity_id"], anchor["chunk_id"], anchor["start"],
                    anchor["end"], anchor["quote_exact"],
                )
                or resolved["mention_id"] in resolved_mention_ids
                or lifecycle.get("status") != "accepted"
                or review.get("state") != "approved"
                or review.get("reviewer_kind") != "independent_model_literary_qa"
                or row.get("created_by_activity_ref") == review.get("approver_activity_ref")
            ):
                raise CurationError(f"invalid endpoint resolution governance: {resolution_id}")
            _verify_anchor(anchor, chunks, resolution_id)
            if anchor["quote_exact"] != chunks[anchor["chunk_id"]]["text"][anchor["start"]:anchor["end"]]:
                raise CurationError(f"endpoint resolution does not match source slice: {resolution_id}")
            resolved_mention_ids.add(resolved["mention_id"])
            resolutions[resolution_id] = row
            connection.execute(
                "INSERT INTO endpoint_resolutions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resolution_id, previous.get("mention_id"), resolved["mention_id"],
                    previous.get("entity_id"), resolved["entity_id"], anchor["chapter_id"],
                    anchor["chunk_id"], anchor["start"], anchor["end"], anchor["quote_exact"],
                    anchor["chunk_text_sha256"], anchor["path"], lifecycle["status"],
                    review["state"], review["reviewer_kind"], review["approver_activity_ref"],
                    review["rationale"], row["created_by_activity_ref"], canonical_json(row),
                ),
            )

        relation_paths = relation_inputs["accepted"]
        accepted_rows = [row for path in relation_paths for _, row in iter_jsonl(path)]
        occurrence_rows = [row for row in accepted_rows if row.get("record_type") == "relation_occurrence"]
        episode_rows = [row for row in accepted_rows if row.get("record_type") == "relation_episode"]
        if not occurrence_rows or not episode_rows:
            raise CurationError("accepted relation ledger must contain occurrences and episodes")
        if len(occurrence_rows) + len(episode_rows) != len(accepted_rows):
            raise CurationError("unknown record type in accepted relation ledger")
        if len({row["occurrence_id"] for row in occurrence_rows}) != len(occurrence_rows):
            raise CurationError("duplicate accepted relation occurrence ID")
        if len({row["relation_episode_id"] for row in episode_rows}) != len(episode_rows):
            raise CurationError("duplicate accepted relation episode ID")

        review_rows = [
            row
            for path in relation_inputs["review_batches"].values()
            for _, row in iter_jsonl(path)
        ]
        reviews = {row["candidate_id"]: row for row in review_rows}
        if not review_rows:
            raise CurationError("independent review ledgers are empty")
        if len(reviews) != len(review_rows):
            raise CurationError("duplicate candidate ID in independent review ledgers")
        original_reviews = reviews

        refinement_reviews: list[dict[str, Any]] = []
        identity_reviews: list[dict[str, Any]] = []
        refinement_ids: set[str] = set()
        refined_source_ids: set[str] = set()

        for round_number, review_path in relation_inputs["refinement_reviews"].items():
            round_reviews = [row for _, row in iter_jsonl(review_path)]
            round_accepted = [
                row
                for _, row in iter_jsonl(
                    relation_inputs["refinement_accepted"][round_number]
                )
            ]
            if not round_reviews or not round_accepted:
                raise CurationError(f"empty refinement round {round_number}")
            expected_occurrence_ids: list[str] = []
            expected_episode_ids: list[str] = []
            for row in round_reviews:
                refinement_id = row["refinement_id"]
                source_id = row["source_candidate_id"]
                if refinement_id in refinement_ids or source_id in refined_source_ids:
                    raise CurationError(f"duplicate refinement review lineage: {refinement_id}")
                refinement_ids.add(refinement_id)
                refined_source_ids.add(source_id)
                if (
                    row.get("verdict") != "pass"
                    or row.get("independent_of_creator") is not True
                    or row.get("reviewer_kind") != "independent_model_literary_qa"
                    or original_reviews.get(source_id, {}).get("verdict") != "refine"
                ):
                    raise CurationError(f"invalid refinement review lineage: {refinement_id}")
                promoted = row.get("promoted_occurrence_ids", [])
                if not promoted or len(set(promoted)) != len(promoted):
                    raise CurationError(f"invalid refinement occurrence promotion: {refinement_id}")
                expected_occurrence_ids.extend(promoted)
                if row.get("promoted_episode_id"):
                    expected_episode_ids.append(row["promoted_episode_id"])
            if len(set(expected_occurrence_ids)) != len(expected_occurrence_ids):
                raise CurationError(f"duplicate promoted occurrence in refinement round {round_number}")
            if len(set(expected_episode_ids)) != len(expected_episode_ids):
                raise CurationError(f"duplicate promoted episode in refinement round {round_number}")
            actual_occurrence_ids = [
                row["occurrence_id"]
                for row in round_accepted
                if row.get("record_type") == "relation_occurrence"
            ]
            actual_episode_ids = [
                row["relation_episode_id"]
                for row in round_accepted
                if row.get("record_type") == "relation_episode"
            ]
            if (
                len(actual_occurrence_ids) + len(actual_episode_ids) != len(round_accepted)
                or set(expected_occurrence_ids) != set(actual_occurrence_ids)
                or set(expected_episode_ids) != set(actual_episode_ids)
                or len(set(actual_occurrence_ids)) != len(actual_occurrence_ids)
                or len(set(actual_episode_ids)) != len(actual_episode_ids)
            ):
                raise CurationError(
                    f"refinement round {round_number} does not exactly cover promoted records"
                )
            refinement_reviews.extend(round_reviews)

        for round_number, review_path in relation_inputs["identity_reviews"].items():
            round_reviews = [row for _, row in iter_jsonl(review_path)]
            round_accepted = [
                row
                for _, row in iter_jsonl(relation_inputs["identity_accepted"][round_number])
            ]
            if not round_reviews or not round_accepted:
                raise CurationError(f"empty identity refinement round {round_number}")
            expected_occurrence_ids: list[str] = []
            expected_episode_ids: list[str] = []
            for row in round_reviews:
                refinement_id = row["refinement_id"]
                source_id = row["source_candidate_id"]
                if refinement_id in refinement_ids or source_id in refined_source_ids:
                    raise CurationError(f"duplicate identity refinement lineage: {refinement_id}")
                refinement_ids.add(refinement_id)
                refined_source_ids.add(source_id)
                if (
                    row.get("verdict") != "pass"
                    or row.get("independent_of_creator") is not True
                    or row.get("reviewer_kind") != "independent_model_literary_qa"
                    or original_reviews.get(source_id, {}).get("verdict") != "block"
                ):
                    raise CurationError(f"invalid identity refinement lineage: {refinement_id}")
                promoted = row.get("promoted_occurrence_ids", [])
                if not promoted or len(set(promoted)) != len(promoted):
                    raise CurationError(f"invalid identity occurrence promotion: {refinement_id}")
                expected_occurrence_ids.extend(promoted)
                if row.get("promoted_episode_id"):
                    expected_episode_ids.append(row["promoted_episode_id"])
            if len(set(expected_occurrence_ids)) != len(expected_occurrence_ids):
                raise CurationError(
                    f"duplicate promoted occurrence in identity refinement round {round_number}"
                )
            if len(set(expected_episode_ids)) != len(expected_episode_ids):
                raise CurationError(
                    f"duplicate promoted episode in identity refinement round {round_number}"
                )
            actual_occurrence_ids = [
                row["occurrence_id"]
                for row in round_accepted
                if row.get("record_type") == "relation_occurrence"
            ]
            actual_episode_ids = [
                row["relation_episode_id"]
                for row in round_accepted
                if row.get("record_type") == "relation_episode"
            ]
            if (
                len(actual_occurrence_ids) + len(actual_episode_ids) != len(round_accepted)
                or set(expected_occurrence_ids) != set(actual_occurrence_ids)
                or set(expected_episode_ids) != set(actual_episode_ids)
                or len(set(actual_occurrence_ids)) != len(actual_occurrence_ids)
                or len(set(actual_episode_ids)) != len(actual_episode_ids)
            ):
                raise CurationError(
                    f"identity refinement round {round_number} does not exactly cover promoted records"
                )
            identity_reviews.extend(round_reviews)

        for row in sorted(occurrence_rows, key=lambda item: item["occurrence_id"]):
            if row["occurrence_id"] != expected_occurrence_id(row):
                raise CurationError(f"relation occurrence ID mismatch: {row['occurrence_id']}")
            bindings = row.get("endpoint_bindings", [])
            binding_map = {binding.get("role"): binding for binding in bindings}
            if (
                len(bindings) != 2
                or set(binding_map) != {"subject", "object"}
                or binding_map["subject"].get("entity_id") != row["subject_entity_id"]
                or binding_map["object"].get("entity_id") != row["object_entity_id"]
            ):
                raise CurationError(f"invalid relation endpoint bindings: {row['occurrence_id']}")
            for binding in bindings:
                binding_kind = binding.get("binding_kind")
                mention_id = binding.get("mention_id")
                resolution_id = binding.get("resolution_id")
                if not str(mention_id or "").startswith("mnt."):
                    raise CurationError(f"invalid endpoint mention ID: {row['occurrence_id']}")
                if binding_kind == "mention":
                    if resolution_id is not None:
                        raise CurationError(f"ordinary mention cannot cite a resolution: {row['occurrence_id']}")
                elif binding_kind == "identity_resolution":
                    resolution = resolutions.get(str(resolution_id))
                    if (
                        resolution is None
                        or resolution["resolved_binding"]["mention_id"] != mention_id
                        or resolution["resolved_binding"]["entity_id"] != binding["entity_id"]
                        or resolution["evidence_anchor"]["chunk_id"] != row["evidence_anchor"]["chunk_id"]
                        or not (
                            row["evidence_anchor"]["start"] <= resolution["evidence_anchor"]["start"]
                            < resolution["evidence_anchor"]["end"] <= row["evidence_anchor"]["end"]
                        )
                    ):
                        raise CurationError(f"invalid identity-resolution binding: {row['occurrence_id']}")
                else:
                    raise CurationError(f"unknown endpoint binding kind: {row['occurrence_id']}")
            lifecycle = row["lifecycle"]
            review = lifecycle["review"]
            if lifecycle.get("status") != "accepted" or review.get("state") != "approved":
                raise CurationError(f"non-approved row in accepted ledger: {row['occurrence_id']}")
            if row["created_by_activity_ref"] == review["approver_activity_ref"]:
                raise CurationError(f"creator approved its own relation: {row['occurrence_id']}")
            anchor = row["evidence_anchor"]
            _verify_anchor(anchor, chunks, row["occurrence_id"])
            connection.execute(
                "INSERT INTO relation_occurrences VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["occurrence_id"], row["subject_entity_id"], row["predicate"], row["object_entity_id"],
                    row["polarity"], row["modality"], row["certainty"], row["assertion_scope"],
                    row["evidence_class"], canonical_json(row["perspective"]), row["temporal_context_id"],
                    anchor["chapter_id"], anchor["chunk_id"], anchor["start"], anchor["end"],
                    anchor["quote_exact"], anchor["chunk_text_sha256"], anchor["path"],
                    lifecycle["status"], review["state"], review["approver_activity_ref"],
                    review["rationale"], row["created_by_activity_ref"], canonical_json(row),
                ),
            )
            _insert_qualifiers(
                connection, "relation_occurrence_qualifiers", "occurrence_id",
                row["occurrence_id"], row.get("qualifiers", []),
            )
            for role in ("subject", "object"):
                binding = binding_map[role]
                connection.execute(
                    "INSERT INTO relation_endpoint_bindings VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["occurrence_id"], role, binding["binding_kind"], binding["entity_id"],
                        binding.get("mention_id"), binding.get("resolution_id"), canonical_json(binding),
                    ),
                )

        for row in sorted(episode_rows, key=lambda item: item["relation_episode_id"]):
            if row["relation_episode_id"] != expected_episode_id(row):
                raise CurationError(f"relation episode ID mismatch: {row['relation_episode_id']}")
            lifecycle = row["lifecycle"]
            review = lifecycle["review"]
            if lifecycle.get("status") != "accepted" or review.get("state") != "approved":
                raise CurationError(f"non-approved episode in accepted ledger: {row['relation_episode_id']}")
            if row["created_by_activity_ref"] == review["approver_activity_ref"]:
                raise CurationError(f"creator approved its own episode: {row['relation_episode_id']}")
            connection.execute(
                "INSERT INTO relation_episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row["relation_episode_id"], row["relation_key"], row["subject_entity_id"],
                    row["predicate"], row["object_entity_id"], row["polarity"],
                    canonical_json(row["story_time"]), row["state_at_close"], row["evidence_class"],
                    row["summary"], lifecycle["status"], review["state"], review["approver_activity_ref"],
                    review["rationale"], row["created_by_activity_ref"], canonical_json(row),
                ),
            )
            _insert_qualifiers(
                connection, "relation_episode_qualifiers", "relation_episode_id",
                row["relation_episode_id"], row.get("qualifiers", []),
            )
            for occurrence_id in row.get("support_occurrence_refs", []):
                connection.execute(
                    "INSERT INTO relation_episode_support VALUES (?, ?, 'support')",
                    (row["relation_episode_id"], occurrence_id),
                )
            for occurrence_id in row.get("challenge_occurrence_refs", []):
                connection.execute(
                    "INSERT INTO relation_episode_support VALUES (?, ?, 'challenge')",
                    (row["relation_episode_id"], occurrence_id),
                )

        for batch_id, path in relation_inputs["review_batches"].items():
            batch_review_count = 0
            for _, row in iter_jsonl(path):
                batch_review_count += 1
                if row.get("batch_id") != batch_id:
                    raise CurationError(
                        f"review row is stored in the wrong batch file: {row.get('candidate_id')}"
                    )
            if not batch_review_count:
                raise CurationError(f"empty relation review batch: {batch_id}")
        pass_ids = {candidate_id for candidate_id, row in reviews.items() if row["verdict"] == "pass"}
        promotion_rows = [
            row for _, row in iter_jsonl(relation_inputs["promotion_specs"])
        ]
        promotion_by_candidate = {row["candidate_id"]: row for row in promotion_rows}
        if (
            len(promotion_by_candidate) != len(promotion_rows)
            or set(promotion_by_candidate) != pass_ids
        ):
            raise CurationError("promotion specs do not exactly cover passed candidates")

        candidate_count = 0
        seen_candidate_ids: set[str] = set()
        candidates: dict[str, dict[str, Any]] = {}
        for batch_id, batch_path in relation_inputs["pending_batches"].items():
            batch_candidate_count = 0
            for _, row in iter_jsonl(batch_path):
                batch_candidate_count += 1
                if row.get("review_status") == "accepted":
                    raise CurationError(f"accepted row is forbidden in review queue: {row['id']}")
                if row["id"] in seen_candidate_ids or row["id"] not in reviews:
                    raise CurationError(f"candidate/review coverage mismatch: {row['id']}")
                seen_candidate_ids.add(row["id"])
                review = reviews[row["id"]]
                if review["batch_id"] != batch_id or review.get("independent_of_creator") is not True:
                    raise CurationError(f"invalid independent review binding: {row['id']}")
                evidence = row["evidence"]
                anchor = {
                    "chapter_id": evidence["chunk_id"].split("_", 1)[0],
                    "chunk_id": evidence["chunk_id"],
                    "chunk_text_sha256": evidence["chunk_text_sha256"],
                    "start": evidence["start"],
                    "end": evidence["end"],
                    "quote_exact": evidence["quote_exact"],
                }
                _verify_anchor(anchor, chunks, f"candidate {row['id']}")
                connection.execute(
                    "INSERT INTO relation_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row["id"], batch_id, review["verdict"], row["episode_policy"], row["subject"],
                        row["predicate"], row["object"], row.get("certainty", "unspecified"), row["assertion_scope"],
                        row.get("state_at_close"), anchor["chapter_id"], anchor["chunk_id"], anchor["start"],
                        anchor["end"], anchor["quote_exact"], anchor["chunk_text_sha256"], row.get("confidence"),
                        review["rationale"], canonical_json({"candidate": row, "independent_review": review}),
                    ),
                )
                _insert_qualifiers(
                    connection, "relation_candidate_qualifiers", "candidate_id",
                    row["id"], row.get("qualifiers", []),
                )
                candidate_count += 1
                candidates[row["id"]] = row
            if not batch_candidate_count:
                raise CurationError(f"empty pending relation batch: {batch_id}")
        if seen_candidate_ids != set(reviews):
            raise CurationError("review ledger contains an unknown candidate")

        for candidate_id in sorted(pass_ids):
            candidate = candidates[candidate_id]
            evidence = candidate["evidence"]
            promotion = promotion_by_candidate[candidate_id]
            matches = [
                row
                for row in occurrence_rows
                if row["subject_entity_id"] == candidate["subject"]
                and row["predicate"] == candidate["predicate"]
                and row["object_entity_id"] == candidate["object"]
                and canonical_json(row.get("qualifiers", []))
                == canonical_json(candidate.get("qualifiers", []))
                and row["evidence_anchor"]["chunk_id"] == evidence["chunk_id"]
                and row["evidence_anchor"]["start"] == evidence["start"]
                and row["evidence_anchor"]["end"] == evidence["end"]
                and row["evidence_anchor"]["quote_exact"] == evidence["quote_exact"]
                and canonical_json(row["perspective"])
                == canonical_json(promotion["perspective"])
            ]
            if len(matches) != 1:
                raise CurationError(
                    f"passed candidate does not map to exactly one accepted occurrence: {candidate_id}"
                )

        gold_rows = [
            row
            for path in relation_inputs["gold"].values()
            for _, row in iter_jsonl(path)
        ]
        if not gold_rows or len({row["gold_id"] for row in gold_rows}) != len(gold_rows):
            raise CurationError("relation gold ledgers contain duplicate case IDs")
        refinement_by_source = {
            row["source_candidate_id"]: row for row in refinement_reviews + identity_reviews
        }
        for gold in gold_rows:
            gold_id = gold["gold_id"]
            source_case_id = gold["source_case_id"]
            if (
                gold.get("requires_new_literary_judgment") is not False
                or gold.get("reviewer_kind") != "independent_model_literary_qa"
                or source_case_id not in reviews
                or reviews[source_case_id]["verdict"] != gold["expected_verdict"]
            ):
                raise CurationError(f"invalid relation gold provenance: {gold_id}")
            if gold["case_kind"] == "refinement_lineage":
                resolution = refinement_by_source.get(source_case_id)
                if resolution is None or gold.get("resolution_verdict") != resolution["verdict"]:
                    raise CurationError(f"invalid relation refinement gold: {gold_id}")
            elif gold["expected_verdict"] != "pass" and (
                gold["expected_occurrence_ids"] or gold["expected_episode_ids"]
            ):
                raise CurationError(f"non-pass gold leaked fact IDs: {gold_id}")

            expected_relation = gold.get("expected_relation")
            for occurrence_id in gold["expected_occurrence_ids"]:
                occurrence = connection.execute(
                    "SELECT subject_entity_id,predicate,object_entity_id,raw_json "
                    "FROM relation_occurrences WHERE occurrence_id=?",
                    (occurrence_id,),
                ).fetchone()
                if occurrence is None:
                    raise CurationError(f"gold occurrence missing from facts: {gold_id}")
                if expected_relation and (
                    occurrence["subject_entity_id"],
                    occurrence["predicate"],
                    occurrence["object_entity_id"],
                ) != (
                    expected_relation["subject"],
                    expected_relation["predicate"],
                    expected_relation["object"],
                ):
                    raise CurationError(f"gold occurrence relation mismatch: {gold_id}")
                if expected_relation and json.loads(occurrence["raw_json"])["qualifiers"] != expected_relation["qualifiers"]:
                    raise CurationError(f"gold occurrence qualifier mismatch: {gold_id}")
            for episode_id in gold["expected_episode_ids"]:
                episode = connection.execute(
                    "SELECT subject_entity_id,predicate,object_entity_id,state_at_close,story_time_json "
                    "FROM relation_episodes WHERE relation_episode_id=?",
                    (episode_id,),
                ).fetchone()
                if episode is None:
                    raise CurationError(f"gold episode missing from facts: {gold_id}")
                if expected_relation and (
                    episode["subject_entity_id"], episode["predicate"], episode["object_entity_id"]
                ) != (
                    expected_relation["subject"], expected_relation["predicate"], expected_relation["object"]
                ):
                    raise CurationError(f"gold episode relation mismatch: {gold_id}")
                if gold.get("expected_episode_state") and episode["state_at_close"] != gold["expected_episode_state"]:
                    raise CurationError(f"gold episode state mismatch: {gold_id}")
                if gold.get("expected_story_time_kind") and json.loads(episode["story_time_json"])["kind"] != gold["expected_story_time_kind"]:
                    raise CurationError(f"gold episode time mismatch: {gold_id}")

        source_rows: list[tuple[str, str, str, int, int | None]] = []
        for path, role, expected_count in input_inventory:
            count = expected_count
            if path.suffix == ".jsonl" and count is None:
                count = sum(1 for _ in iter_jsonl(path))
            source_rows.append((relpath(path), role, sha256_file(path), path.stat().st_size, count))
        for row in source_rows:
            connection.execute("INSERT INTO source_files VALUES (?, ?, ?, ?, ?)", row)

        counts = {
            "chapters": len(chapter_rows),
            "chunks": len(chunk_index_rows),
            "chapter_summaries": summary_count,
            "entities": len(entity_rows),
            "naming_decisions": len(naming_rows),
            "naming_candidates": connection.execute("SELECT count(*) FROM naming_candidates").fetchone()[0],
            "endpoint_resolutions": len(resolution_rows),
            "relation_occurrences": len(occurrence_rows),
            "relation_episodes": len(episode_rows),
            "relation_keys": connection.execute("SELECT count(DISTINCT relation_key) FROM relation_episodes").fetchone()[0],
            "relation_candidates": candidate_count,
        }
        coverage = graph_coverage(connection)
        metadata = {
            "artifact": "literary_knowledge_sqlite",
            "schema_version": "1.1.0",
            "authority": "reviewed_literary_curation_plus_immutable_source",
            "candidate_policy": "separate_table_never_in_accepted_views",
            "counts": counts,
            "relation_gold_cases": len(gold_rows),
            "graph_coverage": coverage,
        }
        for key, value in sorted(metadata.items()):
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?)",
                (key, canonical_json(value) if not isinstance(value, str) else value),
            )

        connection.commit()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if foreign_keys or integrity != "ok":
            raise CurationError(f"SQLite validation failed: foreign_keys={foreign_keys}, integrity={integrity}")
        connection.execute("VACUUM")
        connection.commit()
    except Exception:
        connection.close()
        if temporary.exists():
            temporary.unlink()
        raise
    connection.close()
    os.replace(temporary, output)

    reports = generate_reports(output)
    db_sha = sha256_file(output)
    source_digest = sha256_bytes(
        canonical_json([(path, digest, size) for path, _, digest, size, _ in source_rows]).encode("utf-8")
    )
    manifest = {
        "artifact": "literary_knowledge_build_manifest",
        "schema_version": "1.1.0",
        "database": {"path": relpath(output), "sha256": db_sha, "byte_size": output.stat().st_size},
        "input_file_count": len(source_rows),
        "input_digest": source_digest,
        "counts": counts,
        "relation_gold_cases": len(gold_rows),
        "graph_coverage": coverage,
        "reports": reports,
    }
    atomic_write(DEFAULT_MANIFEST, pretty_json(manifest).encode("utf-8"))
    return manifest


def open_readonly(path: Path = DEFAULT_DB) -> sqlite3.Connection:
    if not path.is_file():
        raise CurationError(f"database does not exist; run build first: {path}")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def validate_database(path: Path = DEFAULT_DB, check_sources: bool = True) -> dict[str, Any]:
    connection = open_readonly(path)
    errors: list[str] = []
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("integrity_check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("foreign_key_check failed")

        stored_counts = json.loads(connection.execute("SELECT value FROM metadata WHERE key='counts'").fetchone()[0])
        actual_counts = {
            "chapters": connection.execute("SELECT count(*) FROM chapters").fetchone()[0],
            "chunks": connection.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "chapter_summaries": connection.execute("SELECT count(*) FROM chapter_summaries").fetchone()[0],
            "entities": connection.execute("SELECT count(*) FROM entities").fetchone()[0],
            "naming_decisions": connection.execute("SELECT count(*) FROM naming_decisions").fetchone()[0],
            "naming_candidates": connection.execute("SELECT count(*) FROM naming_candidates").fetchone()[0],
            "endpoint_resolutions": connection.execute("SELECT count(*) FROM endpoint_resolutions").fetchone()[0],
            "relation_occurrences": connection.execute("SELECT count(*) FROM relation_occurrences").fetchone()[0],
            "relation_episodes": connection.execute("SELECT count(*) FROM relation_episodes").fetchone()[0],
            "relation_keys": connection.execute("SELECT count(DISTINCT relation_key) FROM relation_episodes").fetchone()[0],
            "relation_candidates": connection.execute("SELECT count(*) FROM relation_candidates").fetchone()[0],
        }
        if stored_counts != actual_counts:
            errors.append(f"count mismatch: stored={stored_counts}, actual={actual_counts}")

        accepted_count = connection.execute("SELECT count(*) FROM accepted_relations").fetchone()[0]
        if accepted_count != actual_counts["relation_episodes"]:
            errors.append("accepted_relations view does not exactly equal approved episodes")
        if connection.execute("SELECT count(*) FROM relation_candidates WHERE review_status='accepted'").fetchone()[0]:
            errors.append("accepted candidate leaked into review queue")

        for row in connection.execute(
            "SELECT o.occurrence_id,o.start_offset,o.end_offset,o.quote_exact,o.chunk_text_sha256,c.text,c.text_sha256 "
            "FROM relation_occurrences o JOIN chunks c ON c.chunk_id=o.chunk_id"
        ):
            if row["chunk_text_sha256"] != row["text_sha256"]:
                errors.append(f"stale relation evidence hash: {row['occurrence_id']}")
            if row["text"][row["start_offset"]:row["end_offset"]] != row["quote_exact"]:
                errors.append(f"stale relation evidence slice: {row['occurrence_id']}")
        for row in connection.execute(
            "SELECT r.candidate_id,r.start_offset,r.end_offset,r.quote_exact,r.chunk_text_sha256,c.text,c.text_sha256 "
            "FROM relation_candidates r JOIN chunks c ON c.chunk_id=r.chunk_id"
        ):
            if row["chunk_text_sha256"] != row["text_sha256"]:
                errors.append(f"stale candidate evidence hash: {row['candidate_id']}")
            if row["text"][row["start_offset"]:row["end_offset"]] != row["quote_exact"]:
                errors.append(f"stale candidate evidence slice: {row['candidate_id']}")
        for row in connection.execute(
            "SELECT r.resolution_id,r.start_offset,r.end_offset,r.quote_exact,r.chunk_text_sha256,c.text,c.text_sha256 "
            "FROM endpoint_resolutions r JOIN chunks c ON c.chunk_id=r.chunk_id"
        ):
            if row["chunk_text_sha256"] != row["text_sha256"]:
                errors.append(f"stale endpoint resolution hash: {row['resolution_id']}")
            if row["text"][row["start_offset"]:row["end_offset"]] != row["quote_exact"]:
                errors.append(f"stale endpoint resolution slice: {row['resolution_id']}")
        if connection.execute(
            "SELECT count(*) FROM relation_occurrences o WHERE "
            "(SELECT count(*) FROM relation_endpoint_bindings b WHERE b.occurrence_id=o.occurrence_id) <> 2"
        ).fetchone()[0]:
            errors.append("relation occurrence endpoint bindings are not exactly two per occurrence")

        pending_applied = connection.execute(
            "SELECT n.candidate_id FROM naming_candidates n JOIN entities e ON e.entity_id=n.current_entity_id "
            "WHERE n.status LIKE 'pending%' AND (e.entity_id=n.proposed_entity_id OR e.english_name=n.proposed_english_name)"
        ).fetchall()
        if pending_applied:
            errors.append("pending naming proposal was silently applied")

        if check_sources:
            for row in connection.execute("SELECT path,sha256,byte_size FROM source_files ORDER BY path"):
                try:
                    source = repo_path(row["path"])
                except CurationError as exc:
                    errors.append(str(exc))
                    continue
                if source.stat().st_size != row["byte_size"] or sha256_file(source) != row["sha256"]:
                    errors.append(f"stale input: {row['path']}")
    finally:
        connection.close()
    return {"pass": not errors, "errors": errors, "counts": actual_counts}


def generate_reports(path: Path = DEFAULT_DB) -> list[dict[str, Any]]:
    relation_inputs = _discover_relation_inputs()
    connection = open_readonly(path)
    try:
        entity_lines = [
            "# 实体档案汇编",
            "",
            "> 本文件由文学审定账本编译，只是阅读视图；请勿直接编辑。",
            "> `active` 表示末次证据仍持续，不等于全书结尾状态。",
            "",
        ]
        entities = connection.execute(
            "SELECT entity_id,entity_type,source_name,english_name,naming_status,summary,first_seen_chapter_id,last_seen_chapter_id "
            "FROM entities ORDER BY entity_type, source_name, entity_id"
        ).fetchall()
        for entity in entities:
            aliases = [row[0] for row in connection.execute(
                "SELECT alias FROM entity_aliases WHERE entity_id=? AND alias_kind='source_alias' ORDER BY alias",
                (entity["entity_id"],),
            )]
            neighbors = connection.execute(
                "SELECT n.direction,n.predicate,n.state_at_close,n.relation_episode_id,e.source_name,e.english_name "
                "FROM entity_neighbors n JOIN entities e ON e.entity_id=n.neighbor_entity_id "
                "WHERE n.entity_id=? ORDER BY n.predicate,e.source_name,n.relation_episode_id",
                (entity["entity_id"],),
            ).fetchall()
            occurrences = connection.execute(
                "SELECT o.occurrence_id,o.predicate,o.chapter_id,o.quote_exact,"
                "CASE WHEN o.subject_entity_id=? THEN 'outgoing' ELSE 'incoming' END AS direction,"
                "e.source_name,e.english_name FROM relation_occurrences o "
                "JOIN entities e ON e.entity_id=CASE WHEN o.subject_entity_id=? "
                "THEN o.object_entity_id ELSE o.subject_entity_id END "
                "WHERE o.subject_entity_id=? OR o.object_entity_id=? "
                "ORDER BY o.chapter_id,o.start_offset,o.occurrence_id",
                (entity["entity_id"], entity["entity_id"], entity["entity_id"], entity["entity_id"]),
            ).fetchall()
            candidate_count = connection.execute(
                "SELECT count(*) FROM relation_candidates WHERE subject_entity_id=? OR object_entity_id=?",
                (entity["entity_id"], entity["entity_id"]),
            ).fetchone()[0]
            entity_lines.extend([
                f"## {entity['source_name']} · {entity['english_name'] or '英文名待审'}",
                "",
                f"- ID：`{entity['entity_id']}`",
                f"- 类型：`{entity['entity_type']}`",
                f"- 译名状态：`{entity['naming_status']}`",
                f"- 叙事跨度：`{entity['first_seen_chapter_id']}`—`{entity['last_seen_chapter_id']}`",
                f"- 中文别名：{'、'.join(aliases) if aliases else '无'}",
                f"- 已批准直接证据：{len(occurrences)}",
                f"- 历史或未决关系候选：{candidate_count}（不属于事实）",
                "",
                entity["summary"],
                "",
                "### 已审定关系",
                "",
            ])
            if neighbors:
                entity_lines.extend(["| 方向 | 谓词 | 对端 | 末次状态 | 记录 |", "|---|---|---|---|---|"])
                for row in neighbors:
                    target = f"{row['source_name']} / {row['english_name'] or '英文名待审'}"
                    entity_lines.append(
                        f"| {row['direction']} | `{row['predicate']}` | {target} | `{row['state_at_close']}` | `{row['relation_episode_id']}` |"
                    )
            else:
                entity_lines.append("尚无正式审定的持续关系；这不等于文本中没有关系。")
            entity_lines.extend(["", "### 已批准直接事实与场景", ""])
            if occurrences:
                entity_lines.extend(["| 章节 | 方向 | 谓词 | 对端 | 精确引文 |", "|---|---|---|---|---|"])
                for row in occurrences:
                    quote = row["quote_exact"].replace("\n", " ").replace("|", "\\|")
                    target = f"{row['source_name']} / {row['english_name'] or '英文名待审'}"
                    entity_lines.append(
                        f"| `{row['chapter_id']}` | {row['direction']} | `{row['predicate']}` | "
                        f"{target} | {quote} |"
                    )
            else:
                entity_lines.append("尚无已批准的直接关系证据。")
            entity_lines.append("")

        relation_lines = [
            "# 关系审定表",
            "",
            "> 本文件分别展示 `accepted + approved` 的关系 episode、一次性直接事实与身份端点重解析；待审候选在末尾按批次单列，绝不混入事实表。",
            "",
        ]
        episodes = connection.execute(
            "SELECT r.*,s.source_name AS subject_name,o.source_name AS object_name "
            "FROM accepted_relations r JOIN entities s ON s.entity_id=r.subject_entity_id "
            "JOIN entities o ON o.entity_id=r.object_entity_id "
            "ORDER BY s.source_name,r.predicate,o.source_name,r.relation_episode_id"
        ).fetchall()
        for episode in episodes:
            relation_lines.extend([
                f"## {episode['subject_name']} — `{episode['predicate']}` → {episode['object_name']}",
                "",
                f"- episode：`{episode['relation_episode_id']}`",
                f"- relation key：`{episode['relation_key']}`",
                f"- 末次状态：`{episode['state_at_close']}`",
                f"- 结论：{episode['summary']}",
                "",
                "| 类型 | 章节 | chunk | 精确引文 | occurrence |",
                "|---|---|---|---|---|",
            ])
            evidence = connection.execute(
                "SELECT support_kind,chapter_id,chunk_id,quote_exact,occurrence_id "
                "FROM accepted_relation_evidence WHERE relation_episode_id=? "
                "ORDER BY support_kind,chapter_id,chunk_id,start_offset",
                (episode["relation_episode_id"],),
            ).fetchall()
            for row in evidence:
                quote = row["quote_exact"].replace("\n", " ").replace("|", "\\|")
                relation_lines.append(
                    f"| {row['support_kind']} | `{row['chapter_id']}` | `{row['chunk_id']}` | {quote} | `{row['occurrence_id']}` |"
                )
            relation_lines.append("")

        relation_lines.extend([
            "## 已批准的一次性事实（不合成长时关系）",
            "",
            "> 这些记录是事实，但只证明一次动作、部署、持用或转折；它们不会出现在持续关系邻接图中。",
            "",
            "| 章节 | 主体 | 谓词 | 客体 | 精确引文 | occurrence |",
            "|---|---|---|---|---|---|",
        ])
        direct_only = connection.execute(
            "SELECT o.*,s.source_name AS subject_name,t.source_name AS object_name "
            "FROM relation_occurrences o JOIN entities s ON s.entity_id=o.subject_entity_id "
            "JOIN entities t ON t.entity_id=o.object_entity_id "
            "WHERE NOT EXISTS (SELECT 1 FROM relation_episode_support x WHERE x.occurrence_id=o.occurrence_id) "
            "ORDER BY o.chapter_id,o.start_offset,o.occurrence_id"
        ).fetchall()
        for row in direct_only:
            quote = row["quote_exact"].replace("\n", " ").replace("|", "\\|")
            relation_lines.append(
                f"| `{row['chapter_id']}` | {row['subject_name']} | `{row['predicate']}` | "
                f"{row['object_name']} | {quote} | `{row['occurrence_id']}` |"
            )
        relation_lines.append("")

        relation_lines.extend([
            "## 身份端点重解析",
            "",
            "> 这些记录保留旧自动 mention，同时以独立文学复核批准新的实体端点；不会静默改写历史扫描。",
            "",
            "| 章节 | 原绑定 | 新绑定 | 原文 | resolution |",
            "|---|---|---|---|---|",
        ])
        for row in connection.execute(
            "SELECT r.*,p.source_name AS previous_name,n.source_name AS resolved_name "
            "FROM endpoint_resolutions r LEFT JOIN entities p ON p.entity_id=r.previous_entity_id "
            "JOIN entities n ON n.entity_id=r.resolved_entity_id "
            "ORDER BY r.chapter_id,r.start_offset,r.resolution_id"
        ):
            quote = row["quote_exact"].replace("\n", " ").replace("|", "\\|")
            relation_lines.append(
                f"| `{row['chapter_id']}` | {row['previous_name'] or '未注册组件'} | {row['resolved_name']} | "
                f"{quote} | `{row['resolution_id']}` |"
            )
        relation_lines.append("")

        relation_lines.extend([
            "## 原始候选及终审历史（明确不属于事实）",
            "",
            "> 初次判为 REFINE 的候选仍保留原证据与原裁决；增强证据后的独立复核另行入账，见下一节。",
            "",
            "| 批次 | 数量 | 状态 |",
            "|---|---:|---|",
        ])
        for row in connection.execute(
            "SELECT batch_id,count(*) AS n,group_concat(DISTINCT review_status) AS statuses "
            "FROM relation_candidates GROUP BY batch_id ORDER BY batch_id"
        ):
            relation_lines.append(f"| {row['batch_id']} | {row['n']} | `{row['statuses']}` |")
        relation_lines.append("")

        relation_lines.extend([
            "## 增强证据后已解决的 REFINE",
            "",
            "| 原候选 | 新 episode | 独立复核结论 |",
            "|---|---|---|",
        ])
        refinement_report_paths = list(relation_inputs["refinement_reviews"].values())
        refinement_report_paths.extend(relation_inputs["identity_reviews"].values())
        for review_path in refinement_report_paths:
            for _, row in iter_jsonl(review_path):
                episode = row.get("promoted_episode_id") or "direct-only"
                relation_lines.append(
                    f"| `{row['source_candidate_id']}` | `{episode}` | {row['rationale']} |"
                )
        relation_lines.append("")

        REPORT_ROOT.mkdir(parents=True, exist_ok=True)
        coverage = graph_coverage(connection)
        coverage_lines = [
            "# 关系网覆盖与缺口",
            "",
            "> 本表分别统计已批准的直接事实与持续关系；连通性和分类型覆盖只按 episode 计算。覆盖数字用于暴露缺口，不是完成宣言。",
            "",
            f"- 直接事实触及实体：{coverage['evidence_covered_entities']} / {coverage['total_entities']}",
            f"- 持续关系触及实体：{coverage['covered_entities']} / {coverage['total_entities']}",
            f"- 直接 occurrence：{coverage['accepted_occurrences']}（其中 {coverage['direct_only_occurrences']} 条没有被夸大为持续关系）",
            f"- 唯一关系键：{coverage['accepted_relation_keys']}",
            f"- 连通分量（含孤点）：{coverage['component_count']}",
            f"- 最大连通分量：{coverage['largest_component_entities']} 个实体",
            f"- 零度节点：{coverage['zero_degree_entities']}",
            f"- 一度节点：{coverage['degree_one_entities']}",
            f"- 涉及中心实体的关系键：{coverage['focus_relation_keys']} / {coverage['accepted_relation_keys']}"
            f"（{coverage['focus_relation_keys'] / coverage['accepted_relation_keys']:.1%}）",
            "",
            "## 分类型覆盖",
            "",
            "| 类型 | 已覆盖 | 总数 |",
            "|---|---:|---:|",
        ]
        for entity_type, row in sorted(coverage["type_coverage"].items()):
            coverage_lines.append(f"| `{entity_type}` | {row['covered']} | {row['total']} |")
        coverage_lines.extend(["", "## 尚无持续关系的实体", "", "| 类型 | 中文名 | 英文名 | 未决候选 |", "|---|---|---|---:|"])
        zero_degree = connection.execute(
            "SELECT e.entity_id,e.entity_type,e.source_name,e.english_name,"
            "(SELECT count(*) FROM relation_candidates c WHERE c.subject_entity_id=e.entity_id "
            "OR c.object_entity_id=e.entity_id) AS candidate_count "
            "FROM entities e WHERE NOT EXISTS (SELECT 1 FROM entity_neighbors n WHERE n.entity_id=e.entity_id) "
            "ORDER BY e.entity_type,e.source_name,e.entity_id"
        ).fetchall()
        for row in zero_degree:
            coverage_lines.append(
                f"| `{row['entity_type']}` | {row['source_name']} | {row['english_name'] or '待审'} | {row['candidate_count']} |"
            )
        coverage_lines.append("")

        report_payloads = {
            REPORT_ROOT / "entity_dossiers.md": "\n".join(entity_lines).rstrip() + "\n",
            REPORT_ROOT / "relation_adjudication.md": "\n".join(relation_lines).rstrip() + "\n",
            REPORT_ROOT / "relation_coverage.md": "\n".join(coverage_lines).rstrip() + "\n",
        }
        report_rows = []
        for report_path, content in report_payloads.items():
            atomic_write(report_path, content.encode("utf-8"))
            report_rows.append({"path": relpath(report_path), "sha256": sha256_file(report_path)})
        return report_rows
    finally:
        connection.close()


def _entity_id(connection: sqlite3.Connection, query: str) -> str:
    rows = connection.execute(
        "SELECT DISTINCT e.entity_id FROM entities e LEFT JOIN entity_aliases a ON a.entity_id=e.entity_id "
        "WHERE e.entity_id=? OR e.source_name=? OR e.english_name=? OR a.alias=? ORDER BY e.entity_id",
        (query, query, query, query),
    ).fetchall()
    if not rows:
        raise CurationError(f"entity not found: {query}")
    if len(rows) > 1:
        raise CurationError(f"ambiguous entity surface {query}: {', '.join(row[0] for row in rows)}")
    return rows[0][0]


def command_stats(connection: sqlite3.Connection, _args: argparse.Namespace) -> None:
    counts = json.loads(connection.execute("SELECT value FROM metadata WHERE key='counts'").fetchone()[0])
    print(pretty_json(counts), end="")


def command_coverage(connection: sqlite3.Connection, _args: argparse.Namespace) -> None:
    coverage = json.loads(
        connection.execute("SELECT value FROM metadata WHERE key='graph_coverage'").fetchone()[0]
    )
    print(pretty_json(coverage), end="")


def command_search(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    term = args.text.strip()
    if not term:
        raise CurationError("search text must not be empty")
    rows = connection.execute(
        "SELECT chunk_id,chapter_id,text,instr(text,?) AS pos FROM chunks WHERE instr(text,?)>0 "
        "ORDER BY chapter_id,chunk_index LIMIT ?",
        (term, term, args.limit),
    ).fetchall()
    for row in rows:
        start = max(0, row["pos"] - 41)
        end = min(len(row["text"]), row["pos"] - 1 + len(term) + 80)
        excerpt = row["text"][start:end].replace("\n", " ")
        print(f"{row['chapter_id']} {row['chunk_id']}  {excerpt}")


def command_entity(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    entity_id = _entity_id(connection, args.entity)
    entity = dict(connection.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone())
    entity["aliases"] = [row[0] for row in connection.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id=? AND alias_kind='source_alias' ORDER BY alias", (entity_id,)
    )]
    entity["accepted_relation_count"] = connection.execute(
        "SELECT count(*) FROM entity_neighbors WHERE entity_id=?", (entity_id,)
    ).fetchone()[0]
    entity["pending_candidate_count"] = connection.execute(
        "SELECT count(*) FROM relation_candidates WHERE subject_entity_id=? OR object_entity_id=?", (entity_id, entity_id)
    ).fetchone()[0]
    entity.pop("raw_json", None)
    print(pretty_json(entity), end="")


def command_neighbors(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    entity_id = _entity_id(connection, args.entity)
    rows = connection.execute(
        "SELECT n.*,e.source_name,e.english_name FROM entity_neighbors n "
        "JOIN entities e ON e.entity_id=n.neighbor_entity_id WHERE n.entity_id=? "
        "ORDER BY n.predicate,e.source_name,n.relation_episode_id",
        (entity_id,),
    ).fetchall()
    for row in rows:
        print(
            f"{row['direction']:8} {row['predicate']:30} {row['neighbor_entity_id']:42} "
            f"{row['source_name']} / {row['english_name'] or '英文名待审'} "
            f"[{row['state_at_close']}] {row['relation_episode_id']}"
        )


def command_occurrences(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    entity_id = _entity_id(connection, args.entity)
    rows = connection.execute(
        "SELECT o.*,CASE WHEN o.subject_entity_id=? THEN o.object_entity_id ELSE o.subject_entity_id END AS other_id,"
        "CASE WHEN o.subject_entity_id=? THEN 'outgoing' ELSE 'incoming' END AS direction "
        "FROM relation_occurrences o WHERE o.subject_entity_id=? OR o.object_entity_id=? "
        "ORDER BY o.chapter_id,o.start_offset,o.occurrence_id LIMIT ?",
        (entity_id, entity_id, entity_id, entity_id, args.limit),
    ).fetchall()
    for row in rows:
        quote = row["quote_exact"].replace("\n", " ")
        print(
            f"{row['chapter_id']} {row['direction']:8} {row['predicate']:30} "
            f"{row['other_id']}  {quote}  [{row['occurrence_id']}]"
        )


def command_relation(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    episode = connection.execute(
        "SELECT r.*,s.source_name AS subject_name,o.source_name AS object_name "
        "FROM accepted_relations r JOIN entities s ON s.entity_id=r.subject_entity_id "
        "JOIN entities o ON o.entity_id=r.object_entity_id WHERE r.relation_episode_id=?",
        (args.episode_id,),
    ).fetchone()
    if episode is None:
        raise CurationError(f"accepted relation not found: {args.episode_id}")
    payload = dict(episode)
    payload["evidence"] = [dict(row) for row in connection.execute(
        "SELECT * FROM accepted_relation_evidence WHERE relation_episode_id=? "
        "ORDER BY support_kind,chapter_id,chunk_id,start_offset",
        (args.episode_id,),
    )]
    print(pretty_json(payload), end="")


def command_candidates(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    params: list[Any] = []
    where = ""
    if args.entity:
        entity_id = _entity_id(connection, args.entity)
        where = "WHERE r.subject_entity_id=? OR r.object_entity_id=?"
        params.extend([entity_id, entity_id])
    params.append(args.limit)
    rows = connection.execute(
        "SELECT r.candidate_id,r.batch_id,r.review_status,r.subject_entity_id,r.predicate,r.object_entity_id,"
        "r.chapter_id,r.chunk_id,r.quote_exact FROM relation_candidates r " + where +
        " ORDER BY r.batch_id,r.candidate_id LIMIT ?",
        params,
    ).fetchall()
    for row in rows:
        quote = row["quote_exact"].replace("\n", " ")
        print(
            f"{row['candidate_id']} [{row['review_status']}] {row['subject_entity_id']} "
            f"{row['predicate']} {row['object_entity_id']} @ {row['chunk_id']}\n  {quote}"
        )


def command_path(connection: sqlite3.Connection, args: argparse.Namespace) -> None:
    start = _entity_id(connection, args.source)
    target = _entity_id(connection, args.target)
    row = connection.execute(
        "WITH RECURSIVE edges(a,b,episode_id) AS ("
        " SELECT subject_entity_id,object_entity_id,relation_episode_id FROM accepted_relations"
        " UNION ALL SELECT object_entity_id,subject_entity_id,relation_episode_id FROM accepted_relations"
        "), walk(node,depth,nodes,episodes) AS ("
        " SELECT ?,0,'|'||?||'|',''"
        " UNION ALL"
        " SELECT e.b,w.depth+1,w.nodes||e.b||'|',"
        "        CASE WHEN w.episodes='' THEN e.episode_id ELSE w.episodes||','||e.episode_id END"
        " FROM walk w JOIN edges e ON e.a=w.node"
        " WHERE w.depth < ? AND instr(w.nodes,'|'||e.b||'|')=0"
        ") SELECT nodes,episodes,depth FROM walk WHERE node=? ORDER BY depth,nodes LIMIT 1",
        (start, start, args.max_depth, target),
    ).fetchone()
    if row is None:
        print("no accepted relation path")
        return
    nodes = [item for item in row["nodes"].strip("|").split("|") if item]
    episodes = [item for item in row["episodes"].split(",") if item]
    print(" -> ".join(nodes))
    for episode_id in episodes:
        relation = connection.execute(
            "SELECT subject_entity_id,predicate,object_entity_id FROM accepted_relations WHERE relation_episode_id=?",
            (episode_id,),
        ).fetchone()
        print(f"  {episode_id}: {relation['subject_entity_id']} {relation['predicate']} {relation['object_entity_id']}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build, validate, and query the evidence-backed literary SQLite view."
    )
    result.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="compile reviewed literary curation and source text into SQLite")
    validate = sub.add_parser("validate", help="verify SQLite integrity, freshness, and evidence slices")
    validate.add_argument("--no-source-check", action="store_true")
    sub.add_parser("stats", help="show compiled counts")
    sub.add_parser("coverage", help="show accepted relationship-network coverage")
    search = sub.add_parser("search", help="search the original chunk text")
    search.add_argument("text")
    search.add_argument("--limit", type=int, default=10)
    entity = sub.add_parser("entity", help="show one entity dossier summary")
    entity.add_argument("entity")
    neighbors = sub.add_parser("neighbors", help="show accepted relation neighbors only")
    neighbors.add_argument("entity")
    occurrences = sub.add_parser("occurrences", help="show accepted direct facts and exact quotes for one entity")
    occurrences.add_argument("entity")
    occurrences.add_argument("--limit", type=int, default=50)
    relation = sub.add_parser("relation", help="show one accepted relation and exact evidence")
    relation.add_argument("episode_id")
    candidates = sub.add_parser("candidates", help="explicitly inspect pending relations; never a fact query")
    candidates.add_argument("--entity")
    candidates.add_argument("--limit", type=int, default=20)
    path = sub.add_parser("path", help="navigate accepted relations without inferring a new fact")
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-depth", type=int, default=3)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_database(args.db)
            print(pretty_json(manifest), end="")
            return 0
        if args.command == "validate":
            report = validate_database(args.db, check_sources=not args.no_source_check)
            print(pretty_json(report), end="")
            return 0 if report["pass"] else 1
        connection = open_readonly(args.db)
        try:
            commands = {
                "stats": command_stats,
                "coverage": command_coverage,
                "search": command_search,
                "entity": command_entity,
                "neighbors": command_neighbors,
                "occurrences": command_occurrences,
                "relation": command_relation,
                "candidates": command_candidates,
                "path": command_path,
            }
            commands[args.command](connection, args)
            return 0
        finally:
            connection.close()
    except (CurationError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
