"""Read-only effective/history/hold projection for the selected release."""
from __future__ import annotations

from literary.tools import alignment as a, alignment_release as r, alignment_v2 as v2
from literary.tools.alignment_activate import Publisher, _fresh
from literary.tools.alignment_holds import HoldStore


def managed(root):
    return any((root / name).exists() for name in ("releases/current.json", "releases/registration.json", "releases/activations", "review_holds"))


def selected_state(context, root):
    from literary.tools.alignment_impact import hold_impact
    publisher = Publisher(root)
    context = _fresh(context)
    current = publisher.current(context)
    implicit = current is None
    if implicit:
        current = r.baseline(context, root)
    history = dict(current.history)
    cursor = publisher.pointer()
    seen_releases = {current.digest}
    while cursor:
        if cursor["release_sha256"] not in seen_releases:
            old = publisher.store.load(context, cursor["release_sha256"], baseline_root=root)
            history.update(old.history)
            seen_releases.add(old.digest)
        predecessor = cursor["previous_pointer_sha256"]
        cursor = a.load_json(publisher.store._path("activations", predecessor)) if predecessor else None
    holds = HoldStore(root).active(context, current)
    held_batches = set()
    details = []
    for key, event in holds.items():
        affected = hold_impact(context, current, event["chapter_ids"], event["observation_ids"])
        held_batches.update(affected["batches"])
        details.append({"hold_id": key, "reason": event["reason"], "chapter_ids": event["chapter_ids"],
                        "affected_batch_ids": sorted(affected["batches"]), "classification": event["classification"]})
    held_chapters = {lock["chapter_id"] for batch in held_batches for lock in context.batch_by_id[batch]["chapter_locks"]}
    historical = {stage: sorted({ch["chapter_id"] for row in history.values() if row["stage"] == stage for ch in row["chapters"]})
                  for stage in a.STAGES}
    return current, {"release_sha256": current.digest, "selection_mode": "implicit_legacy_baseline" if implicit else "explicit_release",
                     "history_stage_chapter_ids": historical,
                     "selected_terminal_chapter_ids": sorted(current.terminal_ids),
                     "currently_effective_terminal_chapter_ids": sorted(current.terminal_ids - held_chapters),
                     "held_chapter_ids": sorted(held_chapters), "held_batch_ids": sorted(held_batches), "holds": details}


def check(context, root, *, final=False):
    current, state = selected_state(context, root)
    errors = []
    if final:
        if any(len(current.stages[stage]) != len(context.batch_index) for stage in a.STAGES):
            errors.append("final-check requires all 148 complete selected stage chains")
        if len(state["currently_effective_terminal_chapter_ids"]) != len(context.chapters):
            errors.append("final-check requires all 1439 currently effective terminal chapters")
        if state["holds"]:
            errors.append("final-check rejects unresolved review holds")
        if any(issue.get("_preflight_state") != "completed" for issue in current.issues.values()):
            errors.append("final-check rejects unresolved source issues")
        effective = v2.effective_observation_dispositions(current.stages, current.observations)
        if any(value == "pending" for value in effective.values()):
            errors.append("final-check rejects pending observations")
        references = {"issues": set(), "patches": set(), "observations": set()}
        for row in current.stages["adjudication"].values():
            for ch in row["chapters"]:
                references["issues"].update(ch.get("issue_ids", []))
                references["patches"].update(ch.get("patch_ids", []))
                references["observations"].update(ch.get("accepted_observation_ids", []) + ch.get("rejected_observation_ids", []))
        for kind, referenced in references.items():
            if set(getattr(current, kind)) - referenced:
                errors.append("final-check rejects orphan " + kind)
        if not a._validate_terminal_ledger(context, root, current.stages, current.reading_hashes, errors):
            errors.append("final-check requires a verified derived chapter_alignment.tsv")
    result = {"pass": not errors, "final": final, "batch_count": len(context.batch_index), "batch_index_present": True,
              "stage_batch_counts": {s: len(rows) for s, rows in current.stages.items()},
              "terminal_chapters": len(state["currently_effective_terminal_chapter_ids"]), "total_chapters": len(context.chapters),
              "repair_required": sum(ch.get("final_status") == "repair_required" for row in current.stages["adjudication"].values() for ch in row["chapters"]),
              "selection_and_holds": state}
    return result, errors
