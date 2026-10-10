"""Immutable alignment release resolution. E1: no current pointer or activation.

All files are selected together; old reviews are validated against their own
pins. Temporary materialization is only an adapter to the existing validators,
not a new source. This module cannot author literary judgments or approvals.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from literary.tools import alignment as a
from literary.tools import alignment_v2 as v2


FIXED_FILES = ("manifest.json", "batch_index.jsonl", "compatibility_checkpoint.json")
SOURCE_FILES = ("issues.jsonl", "patches.jsonl", "reading_projection.txt")
FILE_PATTERN = re.compile(r"(?:production|counter_review|adjudication|literary_observations)/(?:volume-\d{2}|FA-V\d{2}-\d{2}(?:\.(?:production|counter_review|adjudication))?)\.jsonl\Z")


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def rows_bytes(rows: list[dict]) -> bytes:
    return b"".join(json_bytes(row) for row in rows)


def rows(payload: bytes) -> list[dict]:
    result = []
    for line in payload.decode("utf-8").splitlines():
        if line.strip():
            item = json.loads(line, object_pairs_hook=a._unique_object)
            a._require(isinstance(item, dict), "release JSONL rows must be objects")
            result.append(item)
    return result


def checked_manifest(value: dict) -> None:
    from literary.tools.alignment_review_versions import DOCUMENT_PATTERN
    a._require(value.get("schema_version") == 1 and value.get("record_type") == "alignment_release",
               "unsupported release contract")
    a._require(value.get("record_sha256") == a.record_sha256(value), "release manifest hash mismatch")
    a._require(isinstance(value.get("reason"), str) and bool(value["reason"].strip()), "release needs an authored reason")
    a._require(set(value.get("stage_selection", {})) == set(a.STAGES), "release must select every stage namespace")
    a._require(isinstance(value.get("files"), dict), "release needs a complete files selection")
    a._require(set(FIXED_FILES + SOURCE_FILES) <= set(value["files"]), "release missing source/structure/checkpoint files")
    for path, digest in value["files"].items():
        a._require(path in FIXED_FILES + SOURCE_FILES or bool(FILE_PATTERN.fullmatch(path))
                   or bool(DOCUMENT_PATTERN.fullmatch(path)), "unapproved release path: " + path)
        a._require(a._is_sha256(digest), "invalid file object hash")
    for stage, selection in value["stage_selection"].items():
        a._require(isinstance(selection, dict), "stage selection must be a batch map")
        a._require(all(a._is_sha256(h) for h in selection.values()), "invalid stage record selection hash")


def stage_selection(files: Mapping[str, bytes]) -> dict:
    selected = {stage: {} for stage in a.STAGES}
    for path, payload in files.items():
        namespace = path.split("/", 1)[0]
        if namespace not in selected:
            continue
        for row in rows(payload):
            batch = row.get("batch_id")
            a._require(batch not in selected[namespace], "ambiguous stages require explicit stage_selection")
            selected[namespace][batch] = row.get("record_sha256")
    return selected


def make_manifest(context: a.Context, files: Mapping[str, bytes], *, reason: str,
                  parent: ReleaseView | None = None, selection: dict | None = None,
                  review_plan: dict | None = None, supersessions: list | None = None) -> dict:
    """Seal mechanical fields around caller-authored files and review metadata."""
    return a.with_record_hash({
        "schema_version": 1, "record_type": "alignment_release", "reason": reason,
        "parent_release_sha256": parent.digest if parent else None,
        "source_sha256": context.manifest["source"]["sha256"],
        "input_manifest_sha256": context.manifest_file_sha256,
        "files": {path: sha(payload) for path, payload in sorted(files.items())},
        "stage_selection": selection if selection is not None else stage_selection(files),
        "review_plan": review_plan if review_plan is not None else {},
        "supersessions": supersessions if supersessions is not None else [],
    })


@dataclass
class ReleaseView:
    manifest: dict
    files: dict[str, bytes]
    stages: dict
    observations: dict
    issues: dict
    patches: dict
    reading_hashes: dict
    projection: bytes
    checkpoint: dict
    history: dict[str, dict] = field(default_factory=dict)
    parent: ReleaseView | None = None

    @property
    def digest(self) -> str:
        return self.manifest["record_sha256"]

    @property
    def terminal_ids(self) -> set[str]:
        return {ch["chapter_id"] for row in self.stages["adjudication"].values()
                for ch in row["chapters"] if ch.get("final_status") in a.TERMINAL_STATUSES}


def authority_files(context: a.Context, root: Path | None = None) -> dict[str, bytes]:
    """Explicit membership; drafts, prose reports, scripts and caches excluded."""
    root = root or context.alignment_root
    result = {}
    for name in FIXED_FILES + ("issues.jsonl", "patches.jsonl"):
        path = root / name
        if path.exists():
            result[name] = path.read_bytes()
        elif name in ("issues.jsonl", "patches.jsonl"):
            result[name] = b""
        elif name == "manifest.json":
            result[name] = (context.alignment_root / name).read_bytes()
        else:
            raise a.AlignmentError("missing baseline authority: " + name)
    result["reading_projection.txt"] = a.reading_projection_path(context, root).read_bytes()
    for namespace in (*a.STAGES, "literary_observations"):
        for path in v2.record_paths(root / namespace):
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def inventory_fingerprint(files: Mapping[str, bytes]) -> str:
    return sha(json_bytes({name: sha(value) for name, value in sorted(files.items())}))


def _ancestral_owners(parent: ReleaseView | None) -> dict:
    owners = {}
    if parent:
        for record in parent.history.values():
            if record.get("schema_version") == 2:
                for chapter in record.get("chapters", []):
                    for lock in chapter.get("observation_locks", []):
                        original = parent.observations.get(lock["observation_id"], {})
                        if (original.get("introduced_by_stage") == record.get("stage")
                                and original.get("producer_task_id") == record.get("task_id")):
                            owners[(lock["observation_id"], lock["record_sha256"])] = record
    return owners


def _materialize(root: Path, files: Mapping[str, bytes], selected: dict, history: dict) -> None:
    found = {stage: {} for stage in a.STAGES}
    for path, payload in files.items():
        namespace = path.split("/", 1)[0]
        if namespace in a.STAGES:
            chosen = []
            records = rows(payload)
            a._require(bool(records), "empty stage files are not allowed: " + path)
            for row in records:
                batch = row.get("batch_id")
                if selected[namespace].get(batch) == row.get("record_sha256"):
                    a._require(batch not in found[namespace], "selected stage appears more than once")
                    found[namespace][batch] = row["record_sha256"]
                    chosen.append(row)
                else:
                    a._require(history.get(row.get("record_sha256")) == row,
                               "unselected stage is not validated ancestral history")
            if not chosen:
                continue
            payload = rows_bytes(chosen)
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    a._require(found == selected, "dangling or wrong selected stage reference")


def _validate(context: a.Context, manifest: dict, files: dict[str, bytes], checkpoint: dict,
              parent: ReleaseView | None) -> ReleaseView:
    from literary.tools.alignment_review_versions import validate_documents, validate_document_input_versions
    documents = validate_documents(context, files, parent)
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="alignment-release-check-") as directory:
        root = Path(directory)
        _materialize(root, files, manifest["stage_selection"], parent.history if parent else {})
        issues = a._load_issues(context, root, errors)
        grouped, reading, applied, projection = a._load_patches(context, root, issues, errors)
        validate_document_input_versions(documents, reading, parent)
        patches = {row["patch_id"]: row for group in grouped.values() for row in group}
        observations = a._load_observations(context, root, errors)
        if files["reading_projection.txt"] != projection:
            errors.append("release reading projection is not deterministic selected patches over immutable source")
        globals_ = {"source_sha256": context.manifest["source"]["sha256"],
                    "issues_sha256": sha(files["issues.jsonl"]), "patches_sha256": sha(files["patches.jsonl"]),
                    "reading_projection_sha256": sha(projection)}
        stages = {stage: a._load_stage(context, root, stage, reading, applied, issues, observations,
                                      patches, globals_, errors, checkpoint) for stage in a.STAGES}
        # Frozen legacy inventory is checked in baseline(), against its own
        # original files. The selected snapshot intentionally excludes retired
        # stage rows and must not masquerade as a rewritten checkpoint.
        v2.validate_observation_references(stages, observations, context, errors,
                                          historical_owners=_ancestral_owners(parent))
    for batch in context.batch_by_id:
        production, counter, adjud = (stages[s].get(batch) for s in a.STAGES)
        if counter and (not production or counter.get("production_record_sha256") != production.get("record_sha256")):
            errors.append(batch + ": counter production reference mismatch")
        if adjud and (not production or not counter or
                      adjud.get("production_record_sha256") != production.get("record_sha256") or
                      adjud.get("counter_record_sha256") != counter.get("record_sha256")):
            errors.append(batch + ": adjudication chain is incomplete or mixed")
        identities = [r.get("task_id") for r in (production, counter, adjud) if r]
        if len(identities) != len(set(identities)):
            errors.append(batch + ": review tasks must be distinct")
    a._require(not errors, "invalid release selection:\n- " + "\n- ".join(errors))
    history = dict(parent.history) if parent else {}
    for mapping in stages.values():
        history.update({row["record_sha256"]: row for row in mapping.values()})
    return ReleaseView(manifest, files, stages, observations, issues, patches, reading, projection,
                       checkpoint, history, parent)


def _baseline_snapshot(context: a.Context, manifest: dict, files: dict[str, bytes]) -> ReleaseView:
    """Validate the stored baseline, never reinterpret it using later issues."""
    checked_manifest(manifest)
    a._require(manifest.get("parent_release_sha256") is None, "baseline cannot have a parent")
    a._require(manifest.get("source_sha256") == context.manifest["source"]["sha256"] and
               manifest.get("input_manifest_sha256") == context.manifest_file_sha256,
               "baseline source/structure pins changed")
    a._require(sha(files["manifest.json"]) == context.manifest_file_sha256, "baseline manifest pin mismatch")
    a._require(files["batch_index.jsonl"] == a.batch_index_bytes(context), "baseline batch index mismatch")
    a._require(stage_selection(files) == manifest["stage_selection"], "baseline must select all original stage rows")
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="alignment-baseline-check-") as directory:
        root = Path(directory)
        for name, payload in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        checkpoint = v2.load_checkpoint(context, root, errors)
    a._require(checkpoint is not None and not errors, "baseline checkpoint invalid: " + "; ".join(errors))
    required_locks = {"source_sha256", "issues_sha256", "patches_sha256", "reading_projection_sha256"}
    a._require(set(checkpoint["artifact_locks"]) == required_locks and
               all(a._is_sha256(value) for value in checkpoint["artifact_locks"].values()) and
               checkpoint["artifact_locks"]["source_sha256"] == context.manifest["source"]["sha256"],
               "baseline checkpoint artifact lock inventory incomplete")
    for name, payload in files.items():
        namespace = name.split("/", 1)[0]
        frozen = (namespace in a.STAGES and any(row.get("schema_version") == 1 for row in rows(payload)))
        if namespace == "literary_observations":
            frozen = any(row.get("observation_id") in checkpoint["observation_hashes"] for row in rows(payload))
        if frozen:
            a._require(checkpoint["legacy_files"].get(name) == sha(payload),
                       "baseline checkpoint missing frozen file membership: " + name)
    view = _validate(context, manifest, files, checkpoint, None)
    v2.validate_checkpoint_inventory(checkpoint, view.stages, view.observations, errors)
    a._require(not errors, "baseline checkpoint inventory invalid: " + "; ".join(errors))
    return view


def baseline(context: a.Context, root: Path | None = None) -> ReleaseView:
    """Read and validate history. Does not create a checkpoint, release or pointer."""
    root = root or context.alignment_root
    report, errors = a.validate_alignment(context, root)
    a._require(report["pass"] and not errors, "baseline history invalid: " + "; ".join(errors))
    files = authority_files(context, root)
    manifest = make_manifest(context, files, reason="Mechanical baseline of existing history; no new reading or approval")
    return _baseline_snapshot(context, manifest, files)


def resolve(context: a.Context, manifest: dict, objects: Mapping[str, bytes], *, parent: ReleaseView) -> ReleaseView:
    """Validate a complete candidate against a validated parent. Never activates."""
    checked_manifest(manifest)
    a._require(manifest.get("parent_release_sha256") == parent.digest, "release parent mismatch")
    a._require(manifest.get("source_sha256") == context.manifest["source"]["sha256"] and
               manifest.get("input_manifest_sha256") == context.manifest_file_sha256, "frozen source/structure pins changed")
    files = {}
    for path, digest in manifest["files"].items():
        a._require(digest in objects and sha(objects[digest]) == digest, "missing or mutated object: " + path)
        files[path] = objects[digest]
    for path in FIXED_FILES:
        a._require(files[path] == parent.files[path], "immutable authority changed: " + path)
    for path, digest in parent.checkpoint["legacy_files"].items():
        a._require(path in files and sha(files[path]) == digest, "frozen historical file changed: " + path)
    view = _validate(context, manifest, files, parent.checkpoint, parent)
    # Unique discoveries cannot vanish or silently mutate; revisions add IDs.
    for key, old in parent.observations.items():
        a._require(key in view.observations and view.observations[key]["record_sha256"] == old["record_sha256"],
                   "historical observation omitted or rewritten: " + key)
    supersessions = manifest.get("supersessions", [])
    a._require(isinstance(supersessions, list), "supersessions must be an explicit list")
    consumed = set()
    for kind in ("issues", "patches"):
        previous, current = getattr(parent, kind), getattr(view, kind)
        for key, old in previous.items():
            if key in current:
                a._require(current[key]["record_sha256"] == old["record_sha256"], "new versions require new " + kind + " ID")
                continue
            matches = [(i, item) for i, item in enumerate(supersessions)
                       if isinstance(item, dict) and item.get("kind") == kind and item.get("old_id") == key
                       and item.get("old_record_sha256") == old["record_sha256"]
                       and item.get("new_id") in current
                       and item.get("new_record_sha256") == current[item["new_id"]]["record_sha256"]
                       and isinstance(item.get("reason"), str) and item["reason"].strip()]
            a._require(len(matches) == 1, "missing exact replacement lineage: " + kind + "/" + key)
            consumed.add(matches[0][0])
    a._require(consumed == set(range(len(supersessions))), "unused, duplicate or invented replacement lineage")
    for stage in a.STAGES:
        a._require(set(parent.stages[stage]) <= set(view.stages[stage]), "selected historical stage cannot be dropped")
    return view


def object_map(files: Mapping[str, bytes]) -> dict[str, bytes]:
    return {sha(payload): payload for payload in files.values()}


class Store:
    """Content-addressed candidate storage. Storing never selects current."""

    def __init__(self, alignment_root: Path):
        self.root = alignment_root / "releases"

    def _path(self, directory: str, digest: str) -> Path:
        a._require(a._is_sha256(digest), "invalid content-addressed identifier")
        return a._repo_path(self.root, f"{directory}/{digest}")

    @staticmethod
    def _put(path: Path, payload: bytes) -> None:
        if path.exists():
            a._require(path.read_bytes() == payload, "immutable object collision or corruption")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".object-", suffix=".pending", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                a._require(path.read_bytes() == payload, "immutable object collision or corruption")
        finally:
            temporary.unlink(missing_ok=True)

    def put_object(self, payload: bytes) -> str:
        digest = sha(payload)
        self._put(self._path("objects", digest), payload)
        return digest

    def get_object(self, digest: str) -> bytes:
        payload = self._path("objects", digest).read_bytes()
        a._require(sha(payload) == digest, "stored object hash mismatch")
        return payload

    def put_manifest(self, manifest: dict) -> str:
        checked_manifest(manifest)
        digest = manifest["record_sha256"]
        self._put(self._path("manifests", digest), json_bytes(manifest))
        return digest

    def get_manifest(self, digest: str) -> dict:
        payload = self._path("manifests", digest).read_bytes()
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=a._unique_object)
        checked_manifest(value)
        a._require(payload == json_bytes(value), "stored manifest bytes changed from canonical immutable form")
        a._require(value["record_sha256"] == digest, "stored manifest name/hash mismatch")
        return value

    def save(self, view: ReleaseView) -> str:
        if view.parent:
            self.save(view.parent)
        for payload in view.files.values():
            self.put_object(payload)
        return self.put_manifest(view.manifest)

    def load(self, context: a.Context, digest: str, *, baseline_root: Path | None = None,
             _seen: set | None = None) -> ReleaseView:
        seen = set() if _seen is None else _seen
        a._require(digest not in seen, "release ancestry cycle")
        seen.add(digest)
        manifest = self.get_manifest(digest)
        objects = {key: self.get_object(key) for key in set(manifest["files"].values())}
        parent_digest = manifest.get("parent_release_sha256")
        if parent_digest is None:
            files = {name: objects[key] for name, key in manifest["files"].items()}
            # Protect the actual frozen history without making unrelated later
            # appends an input to the historical snapshot being replayed.
            root = baseline_root or context.alignment_root
            a._require((root / v2.CHECKPOINT_NAME).read_bytes() == files[v2.CHECKPOINT_NAME],
                       "historical checkpoint bytes changed")
            errors: list[str] = []
            v2.load_checkpoint(context, root, errors)
            a._require(not errors, "historical frozen files changed: " + "; ".join(errors))
            return _baseline_snapshot(context, manifest, files)
        parent = self.load(context, parent_digest, baseline_root=baseline_root, _seen=seen)
        return resolve(context, manifest, objects, parent=parent)
