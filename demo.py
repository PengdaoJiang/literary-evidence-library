"""Original short fiction + synthetic review records for the real compiler.

Fixture activities are not historical human or model reviews. They test the
record contract only. This script never loads either original novel.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_library(root: Path):
    spec = importlib.util.spec_from_file_location("public_literary_library", root / "literary/tools/library.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_fixture(root: Path):
    root.mkdir(parents=True, exist_ok=False)
    for relative in ("literary/tools/library.py", "literary/schema/knowledge.sql"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / relative, destination)
    library = load_library(root)

    def put(path, value):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith(".jsonl"):
            content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in value)
        elif isinstance(value, str):
            content = value
        else:
            content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        target.write_text(content, encoding="utf-8", newline="\n")

    texts = ["清晨，林岑保管白塔的铜钥匙。宁舟与林岑都在桥头避雨，但两人并未结盟。",
             "傍晚，宁舟把灯放在窗边。林岑仍带着铜钥匙，等待守夜人回来。"]
    source_text = "\n".join(texts) + "\n"
    put("source/original/demo.txt", source_text)
    put("source/manifest/source.sha256", library.sha256_bytes(source_text.encode()) + "\n")
    put("source/manifest/source_meta.json", {"data_kind": "original_public_test_fiction", "review_records": "synthetic fixtures"})
    put("dataset.json", {"source_text": "source/original/demo.txt", "focus_entity": "char.lin",
        "expected_counts": {"chapters": 2, "chunks": 2, "naming_decisions": 3}})
    chapters, chunks = [], []
    for n, text in enumerate(texts, 1):
        chapter, chunk = f"CH{n:04}", f"CH{n:04}_001"
        chunk_path = f"build/chunks/{chunk}.json"
        chapters.append({"chapter_id": chapter, "chapter_number": n, "title": f"白塔札记 {n}",
            "volume_title": "公开演示", "path": f"build/chapters/{chapter}.txt", "char_count": len(text), "paragraph_count": 1})
        chunks.append({"chapter_id": chapter, "chunk_id": chunk, "chunk_index": 1,
            "path": chunk_path, "text_sha256": library.sha256_bytes(text.encode())})
        put(chunk_path, {**chunks[-1], "text": text})
        put(f"build/chapters/{chapter}.txt", text + "\n")
    put("build/indices/chapters.jsonl", chapters)
    put("build/indices/chunks.jsonl", chunks)
    put("knowledge/summaries/chapters/demo.jsonl", [
        {"chapter_id": row["chapter_id"], "summary": "Public fixture scene", "key_points": [], "evidence_refs": []} for row in chapters])
    entities = [
        {"entity_id": "char.lin", "entity_type": "character", "name": "林岑", "summary": "白塔看守", "aliases": []},
        {"entity_id": "char.ning", "entity_type": "character", "name": "宁舟", "summary": "旅人", "aliases": []},
        {"entity_id": "item.key", "entity_type": "item", "name": "铜钥匙", "summary": "白塔的钥匙", "aliases": []},
    ]
    put("literary/curation/entities/characters.jsonl", entities[:2])
    put("literary/curation/entities/items.jsonl", entities[2:])
    for name in ("factions", "locations", "extensions"):
        put(f"literary/curation/entities/{name}.jsonl", [])
    put("literary/curation/entities/IDENTITY_DECISIONS.md", "# Fixture identities\n\nThree explicit, distinct identities; no original-novel data.\n")
    put("literary/curation/naming/decisions.jsonl", [
        {"entity_id": entity["entity_id"], "canonical_english_name": english,
         "decision_status": "synthetic_fixture", "decision_batch_id": "DEMO"}
        for entity, english in zip(entities, ("Lin Cen", "Ning Zhou", "Copper Key"))])
    put("literary/curation/naming/policy.json", {"data_kind": "synthetic_fixture"})
    put("literary/curation/naming/pending.jsonl", [])
    put("literary/curation/mentions/alias_policy.json", {"data_kind": "synthetic_fixture"})
    put("literary/curation/mentions/endpoint_resolutions.jsonl", [])
    quote = "林岑保管白塔的铜钥匙"
    start = texts[0].index(quote)
    anchor = {"chapter_id": "CH0001", "chunk_id": "CH0001_001", "start": start, "end": start + len(quote),
              "quote_exact": quote, "chunk_text_sha256": chunks[0]["text_sha256"], "path": chunks[0]["path"]}
    lifecycle = {"status": "accepted", "review": {"state": "approved", "approver_activity_ref": "fixture:reviewer",
        "rationale": "Synthetic contract fixture: the sentence explicitly states custody."}}
    occurrence = {"record_type": "relation_occurrence", "subject_entity_id": "char.lin", "predicate": "custodian_of",
        "object_entity_id": "item.key", "qualifiers": [], "polarity": "positive", "modality": "asserted",
        "perspective": {"kind": "narrator"}, "certainty": "explicit", "assertion_scope": "scene",
        "evidence_class": "synthetic_fixture", "temporal_context_id": "morning", "evidence_anchor": anchor,
        "lifecycle": lifecycle, "created_by_activity_ref": "fixture:producer", "endpoint_bindings": [
            {"role": role, "binding_kind": "mention", "entity_id": eid, "mention_id": f"mnt.demo-{role}", "resolution_id": None}
            for role, eid in (("subject", "char.lin"), ("object", "item.key"))]}
    occurrence["occurrence_id"] = library.expected_occurrence_id(occurrence)
    episode = {"record_type": "relation_episode", "relation_key": "lin-custodian-key", "subject_entity_id": "char.lin",
        "predicate": "custodian_of", "object_entity_id": "item.key", "qualifiers": [], "polarity": "positive",
        "story_time": {"kind": "scene_interval"}, "state_at_close": "active", "evidence_class": "synthetic_fixture",
        "summary": "林岑在这段场景中保管钥匙。", "support_occurrence_refs": [occurrence["occurrence_id"]],
        "challenge_occurrence_refs": [], "lifecycle": lifecycle, "created_by_activity_ref": "fixture:producer"}
    episode["relation_episode_id"] = library.expected_episode_id(episode)
    put("literary/curation/relations/accepted.jsonl", [occurrence, episode])
    candidate = {"id": "DEMO-001", "review_status": "reviewed", "episode_policy": "scene_interval",
        "subject": "char.lin", "predicate": "custodian_of", "object": "item.key", "certainty": "explicit",
        "assertion_scope": "scene", "state_at_close": "active", "confidence": 0.99, "qualifiers": [], "evidence": anchor}
    rejected_quote = "宁舟与林岑都在桥头避雨，但两人并未结盟"
    rejected_start = texts[0].index(rejected_quote)
    rejected_anchor = {**anchor, "start": rejected_start, "end": rejected_start + len(rejected_quote), "quote_exact": rejected_quote}
    rejected = {**candidate, "id": "DEMO-002", "predicate": "ally_of", "object": "char.ning", "evidence": rejected_anchor}
    put("literary/curation/relations/pending/batch_demo.jsonl", [candidate, rejected])
    put("literary/curation/relations/reviews/batch_demo.jsonl", [
        {"candidate_id": "DEMO-001", "batch_id": "DEMO", "verdict": "pass", "independent_of_creator": True,
         "rationale": "Synthetic fixture: explicit custody supports this predicate."},
        {"candidate_id": "DEMO-002", "batch_id": "DEMO", "verdict": "reject", "independent_of_creator": True,
         "rationale": "Synthetic fixture: shared shelter does not establish alliance; the sentence explicitly denies it."}])
    put("literary/curation/relations/reviews/promotion_specs.jsonl", [
        {"candidate_id": "DEMO-001", "perspective": {"kind": "narrator"}}])
    put("literary/curation/relations/gold_round_1.jsonl", [
        {"gold_id": "DEMO-GOLD-001", "source_case_id": "DEMO-001", "requires_new_literary_judgment": False,
         "reviewer_kind": "independent_model_literary_qa", "expected_verdict": "pass", "case_kind": "fixture",
         "expected_occurrence_ids": [occurrence["occurrence_id"]], "expected_episode_ids": [episode["relation_episode_id"]]},
        {"gold_id": "DEMO-GOLD-002", "source_case_id": "DEMO-002", "requires_new_literary_judgment": False,
         "reviewer_kind": "independent_model_literary_qa", "expected_verdict": "reject", "case_kind": "fixture",
         "expected_occurrence_ids": [], "expected_episode_ids": []}])
    return library


def run(root: Path):
    library = make_fixture(root)
    manifest = library.build_database()
    report = library.validate_database()
    if not report["pass"]:
        raise RuntimeError(report["errors"])
    return {"data_kind": "original_test_fiction_with_synthetic_review_records", "validation": report,
            "outputs": "literary/generated/reports", "graph": manifest["graph_coverage"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("demo-output"))
    print(json.dumps(run(parser.parse_args().out), ensure_ascii=False, indent=2))
