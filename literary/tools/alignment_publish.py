#!/usr/bin/env python3
"""Validate and atomically publish one authored v2 batch file, without overwrites.

This utility seals hashes, not literary decisions. Inputs must contain genuine
reader-authored observations or reviews. It never supplies task identities,
reading claims, review rationales, or approval decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from literary.tools import alignment


KINDS = (*alignment.STAGES, "literary_observations")


def _exclusive_publish(source: Path, target: Path) -> None:
    """A hard-link publication is atomic and fails if the target already exists.

    Source and destination must be on the same filesystem. No unsafe fallback to
    a partial write or an overwrite is permitted if hard links are unsupported.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, target)


def _authority_paths(root: Path) -> list[Path]:
    from literary.tools import alignment_v2
    paths = {root / name for name in ("manifest.json", "batch_index.jsonl", "compatibility_checkpoint.json",
                                     "issues.jsonl", "patches.jsonl", "reading_projection.txt")}
    for namespace in (*alignment.STAGES, "literary_observations"):
        paths.update(alignment_v2.record_paths(root / namespace))
    return sorted(path for path in paths if path.is_file())


def _fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _authority_paths(root):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def seal_input(
    context: alignment.Context, kind: str, batch_id: str, input_path: Path,
    observation_stage: str | None = None,
) -> tuple[str, bytes]:
    if kind not in KINDS or batch_id not in context.batch_by_id:
        raise alignment.AlignmentError("unknown kind or batch_id")
    if kind == "literary_observations" and observation_stage not in alignment.STAGES:
        raise alignment.AlignmentError("observations require --observation-stage")
    if kind != "literary_observations" and observation_stage is not None:
        raise alignment.AlignmentError("--observation-stage is only for observations")
    raw = json.loads(input_path.read_text(encoding="utf-8"), object_pairs_hook=alignment._unique_object)
    rows = raw if isinstance(raw, list) else [raw]
    if not rows or (kind != "literary_observations" and len(rows) != 1):
        raise alignment.AlignmentError("input must contain real nonempty observations or exactly one stage record")
    chapters = {row["chapter_id"] for row in context.batch_by_id[batch_id]["chapter_locks"]}
    sealed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("schema_version") != 2:
            raise alignment.AlignmentError("publisher accepts schema_version 2 objects only")
        if kind == "literary_observations":
            if row.get("chapter_id") not in chapters or row.get("introduced_by_stage") != observation_stage:
                raise alignment.AlignmentError("observation is outside the requested batch/stage")
        elif row.get("stage") != kind or row.get("batch_id") != batch_id:
            raise alignment.AlignmentError("stage record does not match the requested target")
        sealed.append(alignment.with_record_hash(row))
    suffix = f".{observation_stage}" if observation_stage else ""
    filename = f"{batch_id}{suffix}.jsonl"
    payload = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for row in sealed
    ).encode("utf-8")
    # JSON serialization preserves exact Unicode text and never synthesizes prose.
    return filename, payload


def publish(
    context: alignment.Context, kind: str, batch_id: str, input_path: Path,
    *, observation_stage: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    from literary.tools.alignment_transaction import writer_lock
    with writer_lock(context.alignment_root):
        if any((context.alignment_root / name).exists() for name in (
            "releases/registration.json", "releases/current.json", "releases/activations",
            "review_holds/head.json", "review_holds/events",
        )):
            raise alignment.AlignmentError("managed release/hold present; use complete-release publication, not a single-file write")
        return _publish(context, kind, batch_id, input_path, observation_stage=observation_stage, dry_run=dry_run)


def _publish(
    context: alignment.Context, kind: str, batch_id: str, input_path: Path,
    *, observation_stage: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    filename, payload = seal_input(context, kind, batch_id, input_path, observation_stage)
    root = context.alignment_root.resolve()
    target = root / kind / filename
    if target.exists():
        raise alignment.AlignmentError("target already exists; published records are immutable, not overwritten")
    lock = root / ".publish.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise alignment.AlignmentError("another publisher or an interrupted publication owns .publish.lock; inspect it before recovery") from exc
    try:
        report, errors = alignment.validate_alignment(context)
        if errors:
            raise alignment.AlignmentError("current ledger is invalid: " + "; ".join(errors[:8]))
        if not (root / "compatibility_checkpoint.json").exists():
            raise alignment.AlignmentError("create and verify the compatibility checkpoint before publishing v2 files")
        before = _fingerprint(root)
        projection = alignment.reading_projection_path(context).read_bytes()
        staging_parent = context.root / "literary/generated"
        staging_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".alignment-publish-", dir=staging_parent) as temporary:
            candidate = Path(temporary) / "alignment"
            candidate.mkdir()
            for source in _authority_paths(root):
                destination = candidate / source.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            (candidate / "reading_projection.txt").write_bytes(projection)
            staged_file = candidate / kind / filename
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            with staged_file.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            candidate_report, errors = alignment.validate_alignment(context, candidate)
            if errors:
                raise alignment.AlignmentError("candidate rejected: " + "; ".join(errors[:12]))
            if before != _fingerprint(root) or projection != alignment.reading_projection_path(context).read_bytes():
                raise alignment.AlignmentError("inputs changed during validation; nothing published")
            if not dry_run:
                _exclusive_publish(staged_file, target)
            return {
                "published": not dry_run,
                "dry_run": dry_run,
                "target": str(target),
                "file_sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "previous_terminal_chapters": report["terminal_chapters"],
                "candidate_terminal_chapters": candidate_report["terminal_chapters"],
                "literary_decisions_created_by_tool": False,
            }
    finally:
        lock.rmdir()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=alignment.ROOT)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--observation-stage", choices=alignment.STAGES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = publish(alignment.load_context(args.root), args.kind, args.batch_id, args.input,
                         observation_stage=args.observation_stage, dry_run=args.dry_run)
    except (alignment.AlignmentError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
