"""Freeze caller-authored candidate files; no reader identities or approvals are generated."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from literary.tools import alignment as a, alignment_release as r, alignment_v2 as v2


def proposal_files(context, parent, path):
    proposal = a.load_json(path)
    a._require(isinstance(proposal, dict) and isinstance(proposal.get("updates", {}), dict), "proposal requires an updates object")
    a._require(proposal.get("parent_release_sha256") == parent.digest, "proposal parent mismatch")
    files = dict(parent.files)
    for name in proposal.get("omit", []):
        a._require(name in files and name not in r.FIXED_FILES, "invalid explicit omission")
        del files[name]
    for name, source in proposal.get("updates", {}).items():
        a._require(name not in r.FIXED_FILES and (name in r.SOURCE_FILES or bool(r.FILE_PATTERN.fullmatch(name))), "unapproved candidate file")
        a._require(isinstance(source, str), "candidate source path must be a string")
        supplied = (path.parent / source).resolve()
        a._require(supplied.is_relative_to(context.root.resolve()) or supplied.is_relative_to(path.parent.resolve()), "candidate input escapes its workspace/draft directory")
        files[name] = supplied.read_bytes()  # One frozen read per submitted input.
    with tempfile.TemporaryDirectory(prefix="alignment-proposal-") as directory:
        root = Path(directory)
        for name in ("issues.jsonl", "patches.jsonl"):
            (root / name).write_bytes(files[name])
        issues, patches, reading, _ = v2.mechanical_inputs(context, root)
        errors = []
        projection = a._load_patches(context, root, issues, errors)[3]
        a._require(not errors, "; ".join(errors))
    if "reading_projection.txt" in proposal.get("updates", {}):
        a._require(files["reading_projection.txt"] == projection, "supplied reading projection is not deterministic")
    files["reading_projection.txt"] = projection
    return proposal, files, issues, patches, reading


def submit(context, parent, path):
    proposal, files, _, _, _ = proposal_files(context, parent, path)
    manifest = r.make_manifest(context, files, parent=parent, reason=proposal.get("reason"),
        selection=proposal.get("stage_selection", parent.manifest["stage_selection"]),
        review_plan=proposal.get("review_plan", {}), supersessions=proposal.get("supersessions", []))
    view = r.resolve(context, manifest, r.object_map(files), parent=parent)
    # Candidate storage is not activation, and cannot make an incomplete review
    # eligible for activation. This is also where inputs become immutable.
    r.Store(context.alignment_root).save(view)
    return view


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("submit", "inspect", "locks"))
    parser.add_argument("--root", type=Path, default=a.ROOT)
    parser.add_argument("--parent")
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--release")
    parser.add_argument("--batch-id")
    args = parser.parse_args()
    try:
        context = a.load_context(args.root)
        store = r.Store(context.alignment_root)
        if args.command == "inspect":
            view = store.load(context, args.release)
            result = {"release_sha256": view.digest, "parent_release_sha256": view.parent.digest if view.parent else None,
                      "selected_stage_counts": {s: len(rows) for s, rows in view.stages.items()},
                      "selected_terminal_chapters": len(view.terminal_ids), "activated_by_this_command": False}
            if view.parent:
                from literary.tools.alignment_impact import impact
                from literary.tools.alignment_review import validate_review_gate
                result["impact"] = impact(context, view.parent, view)
                try:
                    validate_review_gate(context, view)
                    result["review_gate_pass"] = True
                except a.AlignmentError as exc:
                    result.update(review_gate_pass=False, review_gate_error=str(exc))
        else:
            a._require(args.proposal is not None and a._is_sha256(args.parent), "provide --proposal and --parent")
            parent = store.load(context, args.parent)
            if args.command == "submit":
                view = submit(context, parent, args.proposal.resolve())
                result = {"candidate_release_sha256": view.digest, "activated": False}
            else:
                a._require(args.batch_id in context.batch_by_id, "provide a known --batch-id")
                _, _, issues, patches, reading = proposal_files(context, parent, args.proposal.resolve())
                plan = context.batch_by_id[args.batch_id]
                result = {"schema_version": 2, "batch_id": args.batch_id,
                          "input_manifest_sha256": context.manifest_file_sha256, "source_sha256": context.manifest["source"]["sha256"],
                          "batch_index_record_sha256": plan["record_sha256"], "subject_sha256": plan["record_sha256"],
                          "local_dependency_locks": v2.local_dependency_locks(context, args.batch_id, issues, patches, reading),
                          "reading_or_approval_created": False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (a.AlignmentError, OSError, ValueError, TypeError) as exc:
        print("FAIL: " + str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
