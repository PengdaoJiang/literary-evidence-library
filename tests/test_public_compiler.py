from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest

from demo import make_fixture


class PublicCompilerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "workspace"
        self.library = make_fixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def mutate(self, path, change):
        file = self.root / path
        rows = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line.strip()]
        change(rows)
        file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    def test_build_query_and_candidate_isolation(self):
        self.library.build_database()
        self.assertTrue(self.library.validate_database()["pass"])
        db = self.library.open_readonly()
        try:
            self.assertEqual(db.execute("SELECT count(*) FROM accepted_relations").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM relation_candidates").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM entity_neighbors WHERE predicate='ally_of'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM accepted_relation_evidence").fetchone()[0], 1)
        finally:
            db.close()

    def test_wrong_quote_is_rejected(self):
        def change(rows):
            rows[0]["evidence"]["quote_exact"] = "not in the source"
        self.mutate("literary/curation/relations/pending/batch_demo.jsonl", change)
        with self.assertRaisesRegex(self.library.CurationError, "quote does not match"):
            self.library.build_database()

    def test_self_approval_is_rejected(self):
        def change(rows):
            rows[0]["lifecycle"]["review"]["approver_activity_ref"] = "fixture:producer"
        self.mutate("literary/curation/relations/accepted.jsonl", change)
        with self.assertRaisesRegex(self.library.CurationError, "own relation"):
            self.library.build_database()

    def test_dataset_contract_not_silently_inferred(self):
        file = self.root / "dataset.json"
        profile = json.loads(file.read_text())
        profile["expected_counts"]["chapters"] = 3
        file.write_text(json.dumps(profile))
        with self.assertRaisesRegex(self.library.CurationError, "inventory differs"):
            self.library.build_database()

    def test_input_drift_is_reported(self):
        self.library.build_database()
        file = self.root / "literary/curation/entities/IDENTITY_DECISIONS.md"
        file.write_text("Changed after compilation", encoding="utf-8")
        result = self.library.validate_database()
        self.assertFalse(result["pass"])
        self.assertTrue(any("stale input" in error for error in result["errors"]))

    def test_path_escape_is_rejected(self):
        with self.assertRaises(self.library.CurationError):
            self.library.repo_path("../outside.txt")


if __name__ == "__main__":
    unittest.main()
