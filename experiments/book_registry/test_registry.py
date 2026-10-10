"""Protocol tests and local concurrency probes on synthetic text only."""
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import uuid

from contract import empty_batch
from registry import ROOT, Registry

RUN_DIR = ROOT / "outputs/book-registry-tests" / uuid.uuid4().hex
TEXTS = ["甲把玉简交给乙，乙计划次日归还。甲回忆此前借得玉简。", "另一位同名的甲来到城门。"]


def fixture(path, count=2):
    units, paragraphs, offset = [], [], 0
    for i in range(1, count + 1):
        uid = f"SU-{i:06d}"
        units.append({"unit_id": uid, "ordinal": i, "heading": f"合成单元 {i}",
                      "declared_start": 1, "declared_end": 1, "heading_kind": "repeated-number"})
        for j, text in enumerate(TEXTS, 1):
            paragraphs.append({"paragraph_id": f"{uid}-P{j:04d}", "unit_id": uid, "ordinal": j,
                               "source_line": (i - 1) * 2 + j, "snapshot_line": (i - 1) * 2 + j,
                               "char_start": offset, "text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()})
            offset += len(text) + 1
    return Registry.create(path, hashlib.sha256(("\n".join(TEXTS) * count).encode()).hexdigest(), units, paragraphs)


def span(uid, quote, paragraph=1):
    text = TEXTS[paragraph - 1]
    start = text.index(quote)
    return {"paragraph_id": f"{uid}-P{paragraph:04d}", "start": start, "end": start + len(quote), "quote": quote}


def fact(ref, subject, obj, evidence, predicate="交付", kind="relation"):
    return {"ref": ref, "type": kind, "subject": subject, "predicate": predicate, "object": obj,
            "value": None, "polarity": "positive", "modality": "actual", "attribution": "narrator",
            "speaker": None, "frame": "main", "story_time": None, "note": "合成协议测试，不是小说事实", "evidence": [evidence]}


def batch(uid, batch_id="one", complete=True):
    value = empty_batch(batch_id, complete)
    value["model"] = "synthetic-test-no-model-call"
    value["reasoning_effort"] = "none"
    value["entries"] = [{"ref": ref, "existing_id": None, "kind": "person", "name": name,
                         "evidence": [span(uid, name)]} for ref, name in (("a", "甲"), ("b", "乙"))]
    value["mentions"] = [{"entity": "a", "span": span(uid, "甲"), "resolution_note": "合成显式提及"}]
    value["facts"] = [fact("transfer", "a", "b", span(uid, "甲把玉简交给乙"))]
    value["coverage"] = [f"{uid}-P0001", f"{uid}-P0002"]
    return value


def process_worker(path, worker):
    reg = Registry(path)
    done = []
    while leased := reg.claim(worker):
        task = leased[0]
        reg.submit(task["task_id"], task["lease_token"], batch(task["unit_id"]))
        done.append(task["task_id"])
    return done


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.reg = fixture(RUN_DIR / (self._testMethodName + ".sqlite3"))
        self.reg.enqueue()

    def claim(self, worker="worker"):
        return self.reg.claim(worker)[0]

    def submit(self, task, value=None):
        return self.reg.submit(task["task_id"], task["lease_token"], value or batch(task["unit_id"]))

    def test_duplicate_chapter_numbers_and_enqueue_idempotence(self):
        self.assertEqual(self.reg.enqueue()["created"], 0)
        jobs = self.reg.claim("worker", 2)
        self.assertEqual(len({j["task_id"] for j in jobs}), 2)
        self.assertEqual(len({j["unit_id"] for j in jobs}), 2)

    def test_atomic_submit_replay_and_payload_conflict(self):
        task = self.claim()
        original = batch(task["unit_id"])
        receipt = self.submit(task, original)
        self.assertFalse(receipt["idempotent_replay"])
        self.assertTrue(self.submit(task, original)["idempotent_replay"])
        changed = copy.deepcopy(original)
        changed["facts"][0]["note"] = "different"
        with self.assertRaisesRegex(ValueError, "different payload"):
            self.submit(task, changed)
        self.assertEqual(self.reg.status()["counts"]["submissions"], 1)

    def test_bad_quote_rolls_back_every_record(self):
        task = self.claim()
        value = batch(task["unit_id"])
        value["facts"][0]["evidence"][0]["quote"] = "伪造的引文"
        with self.assertRaisesRegex(ValueError, "quote"):
            self.submit(task, value)
        counts = self.reg.status()["counts"]
        self.assertEqual((counts["entities"], counts["facts"], counts["submissions"]), (0, 0, 0))

    def test_expired_lease_is_fenced(self):
        old = self.claim("old")
        with self.reg.connection(True) as con:
            con.execute("UPDATE tasks SET lease_until=0 WHERE task_id=?", (old["task_id"],))
        new = self.claim("new")
        self.assertEqual(old["task_id"], new["task_id"])
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        with self.assertRaisesRegex(ValueError, "Lease"):
            self.submit(old)
        self.submit(new)

    def test_partial_append_coverage_and_completion(self):
        task = self.claim()
        value = batch(task["unit_id"], "first", False)
        value["coverage"] = value["coverage"][:1]
        first = self.submit(task, value)
        self.assertEqual(first["state"], "leased")
        self.assertEqual(self.reg.entry(first["entity_ids"]["a"])["facts"]["total"], 1)
        last = empty_batch("second")
        last["model"] = "synthetic-test-no-model-call"
        last["coverage"] = [f"{task['unit_id']}-P0002"]
        self.assertEqual(self.submit(task, last)["state"], "complete")

    def test_cannot_complete_missing_paragraphs(self):
        task = self.claim()
        value = batch(task["unit_id"])
        value["coverage"] = value["coverage"][:1]
        with self.assertRaisesRegex(ValueError, "not declared"):
            self.submit(task, value)
        self.assertEqual(self.reg.status()["counts"]["entities"], 0)

    def test_successful_yield_requires_new_coverage_and_preserves_failure_allowance(self):
        task = self.claim()
        with self.assertRaisesRegex(ValueError, "new committed"):
            self.reg.yield_task(task["task_id"], task["lease_token"], "no progress")
        value = batch(task["unit_id"], "partial", False)
        value["coverage"] = value["coverage"][:1]
        self.submit(task, value)
        self.assertEqual(self.reg.yield_task(task["task_id"], task["lease_token"], "supervised handoff")["new_paragraphs"], 1)
        with self.reg.connection() as con:
            counts = con.execute("SELECT attempts,max_attempts FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
        self.assertEqual(tuple(counts), (1, 4))
        with self.assertRaisesRegex(ValueError, "Lease"):
            self.reg.yield_task(task["task_id"], task["lease_token"], "replay")
        second = self.reg.claim("resume", task_id=task["task_id"])[0]
        value["batch_id"] = "same-coverage"
        self.submit(second, value)
        with self.assertRaisesRegex(ValueError, "new committed"):
            self.reg.yield_task(second["task_id"], second["lease_token"], "cannot replenish on replay")
        self.reg.release(second["task_id"], second["lease_token"], "real failure still consumes its attempt")
        with self.reg.connection() as con:
            counts = con.execute("SELECT attempts,max_attempts FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
        self.assertEqual(tuple(counts), (2, 4))

    def test_same_name_different_occurrences_not_merged(self):
        one = self.submit(self.claim("one"))
        two = self.submit(self.claim("two"))
        self.assertNotEqual(one["entity_ids"]["a"], two["entity_ids"]["a"])
        self.assertTrue(two["possible_duplicates"])
        self.assertEqual(self.reg.lookup("甲")["total"], 2)

    def test_same_evidence_across_passes_deduplicates(self):
        task = self.claim()
        one = self.submit(task)
        self.reg.enqueue([task["unit_id"]], pass_name="second")
        again = self.reg.claim("worker-2", pass_name="second")[0]
        two = self.submit(again)
        self.assertEqual(one["entity_ids"], two["entity_ids"])
        self.assertEqual(one["fact_ids"], two["fact_ids"])
        self.assertEqual(self.reg.status()["counts"]["facts"], 1)

    def test_explicit_existing_entity_is_reused(self):
        one = self.submit(self.claim())
        second = self.claim()
        value = batch(second["unit_id"])
        value["entries"][0]["existing_id"] = one["entity_ids"]["a"]
        two = self.submit(second, value)
        self.assertEqual(one["entity_ids"]["a"], two["entity_ids"]["a"])
        self.assertEqual(self.reg.entry(one["entity_ids"]["a"])["facts"]["total"], 2)

    def test_out_of_order_arrival_has_source_order(self):
        early, late = self.reg.claim("worker", 2)
        self.submit(late)
        self.submit(early)
        timeline = self.reg.timeline()["items"]
        self.assertEqual([r["evidence"][0]["paragraph_id"] for r in timeline],
                         [f"{early['unit_id']}-P0001", f"{late['unit_id']}-P0001"])
        self.assertEqual(len({r["narrative_key"] for r in timeline}), 2)
        self.assertEqual(self.reg.timeline(as_of=timeline[0]["narrative_end"])["total"], 1)

    def test_story_order_separate_from_narrative_order(self):
        task = self.claim()
        value = batch(task["unit_id"])
        value["entries"] += [{"ref": r, "existing_id": None, "kind": "event", "name": r,
                              "evidence": [span(task["unit_id"], q)]}
                             for r, q in (("transfer_event", "交给乙"), ("borrow_event", "此前借得玉简"))]
        value["facts"].append(fact("earlier_borrow", "borrow_event", "transfer_event",
                                   span(task["unit_id"], "甲回忆此前借得玉简"), "before", "temporal"))
        receipt = self.submit(task, value)
        rows = self.reg.timeline()["items"]
        self.assertEqual(rows[-1]["predicate"], "before")
        self.assertEqual(rows[-1]["subject_id"], receipt["entity_ids"]["borrow_event"])
        self.assertEqual(rows[-1]["review_state"], "pending")

    def test_review_optimistic_concurrency(self):
        receipt = self.submit(self.claim())
        fid = receipt["fact_ids"]["transfer"]
        self.reg.review(fid, "accepted", "test-reviewer", "synthetic verification", 0)
        with self.assertRaisesRegex(ValueError, "revision"):
            self.reg.review(fid, "rejected", "other-reviewer", "stale update", 0)
        self.reg.review(fid, "rejected", "test-reviewer", "reversible correction", 1)
        self.assertEqual(self.reg.timeline()["total"], 0)
        self.assertEqual(self.reg.timeline(review_state="rejected")["items"][0]["review_revision"], 2)
        self.assertEqual(self.reg.entry(receipt["entity_ids"]["a"])["facts"]["total"], 0)

    def test_whole_unit_context_crosses_unit_boundary_but_is_not_evidence(self):
        task = self.claim()
        packet = self.reg.task(task["task_id"], task["lease_token"])
        self.assertEqual(len(packet["context_only"]), 2)
        self.assertTrue(all(p["unit_id"] == "SU-000002" and p["position"] == "after" for p in packet["context_only"]))
        value = batch(task["unit_id"])
        value["facts"][0]["evidence"] = [span("SU-000002", "甲把玉简交给乙")]
        with self.assertRaisesRegex(ValueError, "task body"):
            self.submit(task, value)

    def test_partial_submission_receipts_survive_reclaim(self):
        task = self.claim()
        receipt = self.submit(task, batch(task["unit_id"], complete=False))
        self.reg.release(task["task_id"], task["lease_token"], "synthetic restart")
        next_task = self.claim("replacement")
        packet = self.reg.task(next_task["task_id"], next_task["lease_token"])
        self.assertEqual(packet["prior_submissions"][0]["receipt"]["entity_ids"], receipt["entity_ids"])

    def test_reflexive_identity_and_strict_time_are_rejected(self):
        task = self.claim()
        value = batch(task["unit_id"])
        value["facts"][0].update(type="identity", predicate="different_from", object="a")
        with self.assertRaisesRegex(ValueError, "distinct entity"):
            self.submit(task, value)
        value["entries"][0]["kind"] = "event"
        value["facts"][0].update(type="temporal", predicate="before")
        with self.assertRaisesRegex(ValueError, "before or after itself"):
            self.submit(task, value)
        self.assertEqual(self.reg.status()["counts"]["facts"], 0)

    def test_accepted_only_query_excludes_pending(self):
        receipt = self.submit(self.claim())
        self.assertEqual(self.reg.timeline(review_state="accepted")["total"], 0)
        self.reg.review(receipt["fact_ids"]["transfer"], "accepted", "test", "synthetic verification", 0)
        self.assertEqual(self.reg.timeline(review_state="accepted")["total"], 1)

    def test_compact_quotes_compile_without_inventing_semantic_qualifiers(self):
        from prepare_submission import prepare
        task = self.claim()
        value = batch(task["unit_id"])
        evidence = value["facts"][0]["evidence"][0]
        value["facts"][0]["evidence"] = [{"paragraph_id": evidence["paragraph_id"], "quote": evidence["quote"]}]
        value["facts"][0].pop("story_time")
        value["facts"][0].pop("speaker")
        compiled = prepare(self.reg, task["task_id"], task["lease_token"], value)
        self.assertEqual(compiled["facts"][0]["evidence"][0], evidence)
        self.assertNotIn("start", value["facts"][0]["evidence"][0])
        value["facts"][0].pop("modality")
        with self.assertRaisesRegex(ValueError, "missing"):
            prepare(self.reg, task["task_id"], task["lease_token"], value)
        self.submit(task, compiled)

    def test_compact_quote_ambiguity_must_be_resolved_explicitly(self):
        from prepare_submission import prepare
        task = self.claim()
        value = batch(task["unit_id"])
        value["entries"][0]["evidence"] = [{"paragraph_id": f"{task['unit_id']}-P0001", "quote": "甲"}]
        with self.assertRaisesRegex(ValueError, "matches"):
            prepare(self.reg, task["task_id"], task["lease_token"], value)
        value["entries"][0]["evidence"][0]["occurrence"] = 1
        compiled = prepare(self.reg, task["task_id"], task["lease_token"], value)
        self.assertEqual(compiled["entries"][0]["evidence"][0]["start"], TEXTS[0].rindex("甲"))

    def test_prepare_cli_writes_once_without_submitting(self):
        task = self.claim()
        source = self.reg.path.with_suffix(".compact.json")
        destination = self.reg.path.with_suffix(".prepared.json")
        source.write_text(json.dumps(batch(task["unit_id"]), ensure_ascii=False), encoding="utf-8")
        command = [sys.executable, str(ROOT / "experiments/book_registry/registry.py"), "--db", str(self.reg.path),
                   "prepare", "--task", task["task_id"], "--lease", task["lease_token"],
                   "--file", str(source), "--output", str(destination)]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertFalse(json.loads(completed.stdout)["submitted"])
        self.assertEqual(self.reg.status()["counts"]["submissions"], 0)
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)

    def test_cross_task_evidence_and_unknown_fields_rejected(self):
        task = self.claim()
        value = batch(task["unit_id"])
        value["entries"][0]["evidence"][0]["paragraph_id"] = "SU-000002-P0001"
        with self.assertRaisesRegex(ValueError, "task body"):
            self.submit(task, value)
        value = batch(task["unit_id"])
        value["unexpected"] = 1
        with self.assertRaisesRegex(ValueError, "extra"):
            self.submit(task, value)

    def test_locator_returns_all_matches_and_code_points(self):
        task = self.claim()
        result = self.reg.locate(task["task_id"], task["lease_token"], "甲")
        self.assertEqual(result["total"], 3)
        self.assertTrue(result["ambiguous"])
        result = self.reg.locate(task["task_id"], task["lease_token"], "同名的甲")
        self.assertFalse(result["ambiguous"])
        self.assertEqual(result["matches"][0]["start"], TEXTS[1].index("同名的甲"))
        with self.reg.connection(True) as con:
            # Synthetic-only fixture mutation to exercise a non-BMP code point.
            con.execute("UPDATE paragraphs SET text='😀甲，乙' WHERE paragraph_id=?", (f"{task['unit_id']}-P0002",))
        found = self.reg.locate(task["task_id"], task["lease_token"], "甲", f"{task['unit_id']}-P0002")
        self.assertEqual(found["matches"][0]["start"], 1)

    def test_retry_bound_and_worker_claim_bound(self):
        task = self.claim()
        for i in range(3):
            outcome = self.reg.release(task["task_id"], task["lease_token"], "synthetic failure")
            if i < 2:
                task = self.claim()
        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(self.reg.tasks("failed")["total"], 1)
        self.reg.retry(task["task_id"], "synthetic recovery", 1)
        fresh = self.claim("fresh")
        self.assertEqual(fresh["task_id"], task["task_id"])
        self.submit(fresh)
        with self.assertRaises(ValueError):
            self.reg.claim("worker", count=4)


def concurrency_probe():
    reg = fixture(RUN_DIR / "100-clients.sqlite3", 300)
    reg.enqueue()
    barrier = threading.Barrier(100)

    def worker(number):
        local = Registry(reg.path)
        barrier.wait(timeout=30)
        done = []
        while jobs := local.claim(f"synthetic-client-{number}"):
            task = jobs[0]
            local.lookup("甲", limit=1)
            result = local.submit(task["task_id"], task["lease_token"], batch(task["unit_id"]))
            # Simulate a lost reply and retry the exact same submission.
            replay = local.submit(task["task_id"], task["lease_token"], batch(task["unit_id"]))
            assert replay["idempotent_replay"] and replay["submission_id"] == result["submission_id"]
            done.append(task["task_id"])
        return done

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=100) as executor:
        completed = [tid for result in executor.map(worker, range(100)) for tid in result]
    elapsed = time.perf_counter() - started
    assert len(completed) == len(set(completed)) == 300
    status = reg.status()
    assert status["tasks"] == {"complete": 300} and status["counts"]["submissions"] == 300
    with reg.connection() as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    return {"concurrent_local_clients": 100, "implementation": "threads with independent SQLite connections",
            "tasks_completed_once": 300, "idempotent_replays": 300, "elapsed_seconds": round(elapsed, 3),
            "synthetic_tasks_per_second": round(300 / elapsed, 2), "sqlite_integrity": "ok",
            "journal_mode": status["journal_mode"], "model_calls": 0,
            "limitation": "This is a database/protocol probe, not 100 Luna agents or model throughput."}


def process_probe():
    reg = fixture(RUN_DIR / "8-processes.sqlite3", 40)
    reg.enqueue()
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=8) as executor:
        completed = [tid for result in executor.map(process_worker, [str(reg.path)] * 8,
                                                     [f"process-{i}" for i in range(8)]) for tid in result]
    assert len(completed) == len(set(completed)) == 40
    assert reg.status()["tasks"] == {"complete": 40}
    return {"processes": 8, "tasks_completed_once": 40,
            "elapsed_seconds": round(time.perf_counter() - started, 3), "model_calls": 0}


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProtocolTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    report = {"protocol_tests": result.testsRun, "failures": 0, "errors": 0,
              "thread_concurrency": concurrency_probe(), "process_concurrency": process_probe(),
              "test_databases": str(RUN_DIR), "sqlite_version": sqlite3.sqlite_version}
    out = ROOT / "outputs/book-registry-v1"
    out.mkdir(parents=True, exist_ok=True)
    (out / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
