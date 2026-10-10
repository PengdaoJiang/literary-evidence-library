"""Local, transactional entity registry and bounded chapter leases.

Every operation owns a short SQLite connection. No connection or write lock is
held while a model reads/thinks. All content remains in the research workspace.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import unicodedata
import uuid

from contract import VERSION, validate
from semantic_checks import require_valid

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data/book-registry-v1/registry.sqlite3"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identifier(prefix, value):
    return prefix + "_" + hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()[:32]


def normalize(value):
    return unicodedata.normalize("NFC", value).strip()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def local_path(path):
    path = Path(path).resolve()
    require(any(path.is_relative_to(ROOT / part) for part in ("data", "outputs")),
            "Registry files must stay under this research project's data/ or outputs/")
    return path


class Registry:
    def __init__(self, path=DEFAULT_DB):
        self.path = local_path(path)

    def upgrade_record_reviews(self):
        """Explicit, additive migration; ordinary reads never mutate schemas."""
        sql = Path(__file__).with_name("record_review.sql").read_text(encoding="utf-8")
        with self.connection(True) as con:
            for statement in sql.split(";"):
                if statement.strip():
                    con.execute(statement)
            con.execute("INSERT OR REPLACE INTO metadata VALUES ('record_review_extension','1')")

    @staticmethod
    def visible(record_type, expression):
        # Expressions are internal SQL identifiers, never caller input.
        return f"NOT EXISTS (SELECT 1 FROM record_decisions rd WHERE rd.record_type='{record_type}' AND rd.record_id={expression} AND rd.state='rejected')"

    @classmethod
    def name_surfaces(cls, include_rejected=False):
        """Read naming facts as a derived index; never copy them into aliases.

        A later rejection must remove a derived name even after workers reused
        it. Source positions remain on the fact and its context support.
        """
        names_where = "1" if include_rejected else cls.visible("name", "n.name_id")
        facts_where = "1" if include_rejected else "f.review_state<>'rejected'"
        return f"""name_surfaces AS (
          SELECT n.* FROM names n WHERE {names_where}
          UNION ALL
          SELECT NULL,f.subject_id,json_extract(f.value_json,'$'),
            json_extract(f.value_json,'$'),'fact_'||f.predicate,NULL,NULL,'fact:'||f.fact_id
          FROM facts f WHERE f.fact_type='attribute' AND f.predicate IN ('name','alias','nickname')
            AND f.polarity='positive' AND f.modality='actual'
            AND f.value_json IS NOT NULL AND {facts_where}
        )"""

    @classmethod
    def entity_names(cls, con, entity_id, include_rejected=False):
        return [dict(r) for r in con.execute("WITH " + cls.name_surfaces(include_rejected) +
            " SELECT * FROM name_surfaces WHERE entity_id=? ORDER BY surface,origin", (entity_id,))]

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                              uri=True, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                # The bundled 3.50.4 is affected by the documented WAL-reset bug.
                # Fail rather than silently entering a multi-connection WAL deployment.
                require(con.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                        "This version supports the tested DELETE journal mode only")
                con.execute("PRAGMA synchronous=FULL")
                con.execute("BEGIN IMMEDIATE")
            else:
                con.execute("PRAGMA query_only=ON")
                con.execute("BEGIN")
            require(dict(con.execute("SELECT key,value FROM metadata"))["schema_version"] == VERSION,
                    "Unsupported registry schema")
            yield con
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    @classmethod
    def create(cls, path, source_sha256, units, paragraphs, seeds=None, source_info=None):
        path = local_path(path)
        require(not path.exists(), "Registry already exists; creation never overwrites data")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive file creation also rejects two bootstrap processes racing.
        with path.open("xb"):
            pass
        con = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            con.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            con.executescript(Path(__file__).with_name("record_review.sql").read_text(encoding="utf-8"))
            con.execute("BEGIN IMMEDIATE")
            metadata = {"schema_version": VERSION, "source_sha256": source_sha256,
                        "source_info": encoded(source_info or {}), "created_at": str(time.time())}
            con.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
            con.executemany("INSERT INTO units VALUES (:unit_id,:ordinal,:heading,:declared_start,:declared_end,:heading_kind)", units)
            con.executemany("INSERT INTO paragraphs VALUES (:paragraph_id,:unit_id,:ordinal,:source_line,:snapshot_line,:char_start,:text,:text_sha256)", paragraphs)
            now = time.time()
            kinds = {"sect": "faction", "organization": "faction", "clan": "faction",
                     "artifact": "item", "place": "location", "realm-place": "location",
                     "planet": "location", "continent": "location", "star-domain": "location",
                     "title-person": "title", "state": "concept", "ancient-god": "person"}
            for entry in (seeds or {}).get("entities", []):
                eid = "seed:" + entry["entity_id"]
                con.execute("INSERT INTO entities VALUES (?,?,?,?,?,?)",
                            (eid, kinds.get(entry["category"], entry["category"]), entry["zh_canonical"],
                             "seed_unverified", encoded(entry), now))
                cls._name(con, eid, entry["zh_canonical"], "canonical_seed", entry["first_source_unit"], None, "seed")
            for alias in (seeds or {}).get("aliases", []):
                cls._name(con, "seed:" + alias["entity_id"], alias["zh_alias"], alias["relation_type"],
                          alias["valid_from_unit"] or None, alias["valid_to_unit"] or None, "seed_unverified")
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            # Keep a failed bootstrap visible; never remove an arbitrary database.
            raise
        finally:
            con.close()
        return cls(path)

    @staticmethod
    def _name(con, eid, surface, relation, start, end, origin):
        key = [eid, surface, relation, start, end, origin]
        con.execute("INSERT OR IGNORE INTO names VALUES (?,?,?,?,?,?,?,?)",
                    (identifier("name", key), eid, surface, normalize(surface), relation, start, end, origin))

    def enqueue(self, unit_ids=None, pass_name="extract-v1", first=None, last=None):
        require(bool(pass_name.strip()), "Empty pass name")
        with self.connection(True) as con:
            source = con.execute("SELECT value FROM metadata WHERE key='source_sha256'").fetchone()[0]
            available = {r[0] for r in con.execute("SELECT unit_id FROM units")}
            selected = list(unit_ids) if unit_ids is not None else sorted(available)
            require(set(selected) <= available, "Unknown source unit")
            created = 0
            ids = []
            for uid in selected:
                lo, hi = con.execute("SELECT min(ordinal),max(ordinal) FROM paragraphs WHERE unit_id=?", (uid,)).fetchone()
                start, stop = lo if first is None else first, hi if last is None else last
                require(lo is not None and lo <= start <= stop <= hi, f"Invalid paragraph interval: {uid}")
                count = con.execute("SELECT count(*) FROM paragraphs WHERE unit_id=? AND ordinal BETWEEN ? AND ?",
                                    (uid, start, stop)).fetchone()[0]
                require(count == stop - start + 1, "Non-contiguous paragraph interval")
                tid = identifier("task", [source, uid, start, stop, pass_name, VERSION])
                created += con.execute("INSERT OR IGNORE INTO tasks(task_id,unit_id,first_paragraph,last_paragraph,pass_name,spec_version,created_at) VALUES (?,?,?,?,?,?,?)",
                                       (tid, uid, start, stop, pass_name, VERSION, time.time())).rowcount
                ids.append(tid)
            return {"created": created, "task_ids": ids}

    @staticmethod
    def _lease(con, task_id, token, now):
        row = con.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        require(row is not None, "Unknown task")
        require(row["state"] == "leased" and row["lease_token"] == token and row["lease_until"] > now,
                "Lease expired, superseded, or task is no longer leased")
        return row

    def claim(self, worker, count=1, lease_seconds=1800, pass_name=None, task_id=None):
        require(bool(worker.strip()), "Empty worker ID")
        require(1 <= count <= 3 and 1 <= lease_seconds <= 7200, "Claim 1..3 tasks for at most 7200 seconds")
        with self.connection(True) as con:
            now = time.time()
            expired = con.execute("SELECT task_id,lease_token,attempts,max_attempts FROM tasks WHERE state='leased' AND lease_until<=?", (now,)).fetchall()
            for row in expired:
                con.execute("UPDATE attempts SET outcome='expired',ended_at=? WHERE lease_token=?", (now, row["lease_token"]))
                state = "failed" if row["attempts"] >= row["max_attempts"] else "pending"
                con.execute("UPDATE tasks SET state=?,owner=NULL,lease_token=NULL,lease_until=NULL,last_error='lease expired' WHERE task_id=?", (state, row["task_id"]))
            held = con.execute("SELECT count(*) FROM tasks WHERE owner=? AND state='leased'", (worker,)).fetchone()[0]
            slots = max(0, min(count, 3 - held))
            rows = con.execute("SELECT t.*,u.ordinal,u.heading FROM tasks t JOIN units u USING(unit_id) WHERE state='pending' AND attempts<max_attempts AND (? IS NULL OR pass_name=?) AND (? IS NULL OR task_id=?) ORDER BY u.ordinal,first_paragraph,task_id LIMIT ?", (pass_name, pass_name, task_id, task_id, slots)).fetchall()
            result = []
            for row in rows:
                token = uuid.uuid4().hex
                con.execute("UPDATE tasks SET state='leased',owner=?,lease_token=?,lease_until=?,attempts=attempts+1 WHERE task_id=?",
                            (worker, token, now + lease_seconds, row["task_id"]))
                con.execute("INSERT INTO attempts VALUES (?,?,?,?,NULL,'running')", (token, row["task_id"], worker, now))
                result.append({"task_id": row["task_id"], "unit_id": row["unit_id"], "heading": row["heading"],
                               "first_paragraph": row["first_paragraph"], "last_paragraph": row["last_paragraph"],
                               "pass_name": row["pass_name"], "lease_token": token, "lease_until": now + lease_seconds})
            return result

    def heartbeat(self, task_id, token, lease_seconds=1800):
        require(1 <= lease_seconds <= 7200, "Invalid lease duration")
        with self.connection(True) as con:
            now = time.time()
            self._lease(con, task_id, token, now)
            con.execute("UPDATE tasks SET lease_until=? WHERE task_id=?", (now + lease_seconds, task_id))
            return {"lease_until": now + lease_seconds}

    def release(self, task_id, token, error):
        require(bool(error.strip()), "A release requires an error or reason")
        with self.connection(True) as con:
            now = time.time()
            task = self._lease(con, task_id, token, now)
            state = "failed" if task["attempts"] >= task["max_attempts"] else "pending"
            con.execute("UPDATE attempts SET outcome=?,ended_at=? WHERE lease_token=?",
                        ("failed" if state == "failed" else "released", now, token))
            con.execute("UPDATE tasks SET state=?,owner=NULL,lease_token=NULL,lease_until=NULL,last_error=? WHERE task_id=?",
                        (state, error, task_id))
            return {"state": state}

    def retry(self, task_id, reason, additional_attempts=1):
        require(reason.strip() and 1 <= additional_attempts <= 3, "Explain retry; add only 1..3 attempts")
        with self.connection(True) as con:
            row = con.execute("SELECT state FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            require(row is not None and row[0] == "failed", "Only failed tasks can be explicitly retried")
            con.execute("UPDATE tasks SET state='pending',max_attempts=max_attempts+?,owner=NULL,lease_token=NULL,lease_until=NULL,last_error=? WHERE task_id=?",
                        (additional_attempts, "explicit retry: " + reason, task_id))
            return {"task_id": task_id, "state": "pending", "additional_attempts": additional_attempts}

    def yield_task(self, task_id, token, reason):
        """Return successful partial work without spending its failure allowance.

        A fresh coverage contribution is required. Replaying old coverage cannot
        replenish retries, and the normal failure/expiry path remains unchanged.
        """
        require(bool(reason.strip()), "A yield requires a reason")
        with self.connection(True) as con:
            now = time.time()
            self._lease(con, task_id, token, now)
            added = con.execute("""SELECT count(DISTINCT c.paragraph_id)
                FROM coverage c JOIN submissions s USING(submission_id)
                WHERE c.task_id=? AND s.lease_token=? AND NOT EXISTS (
                  SELECT 1 FROM coverage old JOIN submissions prior USING(submission_id)
                  WHERE old.task_id=c.task_id AND old.paragraph_id=c.paragraph_id
                    AND prior.lease_token<>?)""", (task_id, token, token)).fetchone()[0]
            require(added > 0, "Yield requires new committed paragraph coverage in this lease")
            con.execute("UPDATE attempts SET outcome='released',ended_at=? WHERE lease_token=?", (now, token))
            con.execute("""UPDATE tasks SET state='pending',max_attempts=max_attempts+1,
                owner=NULL,lease_token=NULL,lease_until=NULL,last_error=? WHERE task_id=?""",
                (f"successful yield ({added} new paragraphs): {reason}", task_id))
            return {"task_id": task_id, "state": "pending", "new_paragraphs": added,
                    "failure_allowance_preserved": True}

    def tasks(self, state=None, limit=100, offset=0):
        require(state in (None, "pending", "leased", "complete", "failed") and 1 <= limit <= 1000 and offset >= 0,
                "Invalid task filter or pagination")
        with self.connection() as con:
            total = con.execute("SELECT count(*) FROM tasks WHERE ? IS NULL OR state=?", (state, state)).fetchone()[0]
            rows = [dict(r) for r in con.execute("SELECT task_id,unit_id,first_paragraph,last_paragraph,pass_name,state,attempts,max_attempts,owner,lease_until,last_error FROM tasks WHERE ? IS NULL OR state=? ORDER BY unit_id,first_paragraph,task_id LIMIT ? OFFSET ?", (state, state, limit, offset))]
            return {"total": total, "items": rows, "has_more": offset + len(rows) < total}

    def task(self, task_id, token):
        with self.connection() as con:
            task = dict(self._lease(con, task_id, token, time.time()))
            paragraphs = [dict(r) for r in con.execute("SELECT paragraph_id,ordinal,text FROM paragraphs WHERE unit_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal",
                                                      (task["unit_id"], task["first_paragraph"], task["last_paragraph"]))]
            bounds = con.execute("SELECT min(char_start),max(char_start) FROM paragraphs WHERE unit_id=? AND ordinal BETWEEN ? AND ?",
                                 (task["unit_id"], task["first_paragraph"], task["last_paragraph"])).fetchone()
            # Global neighbours preserve boundary context even for whole chapters.
            before = [dict(r) for r in con.execute("SELECT paragraph_id,unit_id,ordinal,text FROM paragraphs WHERE char_start<? ORDER BY char_start DESC LIMIT 2", (bounds[0],))]
            after = [dict(r) for r in con.execute("SELECT paragraph_id,unit_id,ordinal,text FROM paragraphs WHERE char_start>? ORDER BY char_start LIMIT 2", (bounds[1],))]
            context = [{**p, "position": "before"} for p in reversed(before)] + [{**p, "position": "after"} for p in after]
            covered = [r[0] for r in con.execute("SELECT DISTINCT paragraph_id FROM coverage WHERE task_id=?", (task_id,))]
            prior = [dict(r) for r in con.execute("SELECT submission_id,batch_id,created_at,receipt_json FROM submissions WHERE task_id=? ORDER BY created_at", (task_id,))]
            for item in prior:
                item["receipt"] = json.loads(item.pop("receipt_json"))
            return {"task": task, "source_sha256": con.execute("SELECT value FROM metadata WHERE key='source_sha256'").fetchone()[0],
                    "body": paragraphs, "context_only": context, "previously_declared_coverage": covered,
                    "prior_submissions": prior,
                    "context_warning": "Use whole-book retrospective interpretation, including later revelations. Keep assigned-body evidence and auxiliary context distinct. Prior receipts preserve recovery IDs, not semantic acceptance.",
                    "offset_convention": "0-based Unicode code points [start,end), relative to exact paragraph text including indentation"}

    def locate(self, task_id, token, quote, paragraph_id=None, limit=100):
        require(isinstance(quote, str) and quote.strip() and 1 <= limit <= 1000, "Provide a nonempty exact quote")
        packet = self.task(task_id, token)
        matches, total = [], 0
        for paragraph in packet["body"]:
            if paragraph_id is not None and paragraph["paragraph_id"] != paragraph_id:
                continue
            start = paragraph["text"].find(quote)
            while start >= 0:
                total += 1
                if len(matches) < limit:
                    matches.append({"paragraph_id": paragraph["paragraph_id"], "start": start,
                                    "end": start + len(quote), "quote": quote})
                start = paragraph["text"].find(quote, start + 1)
        return {"total": total, "matches": matches, "ambiguous": total != 1, "has_more": total > limit}

    @staticmethod
    def _span(con, task, span, body_only=True):
        row = con.execute("SELECT * FROM paragraphs WHERE paragraph_id=?", (span["paragraph_id"],)).fetchone()
        require(row is not None, "Unknown evidence paragraph")
        require(not body_only or (row["unit_id"] == task["unit_id"] and task["first_paragraph"] <= row["ordinal"] <= task["last_paragraph"]),
                "Evidence must be inside the task body, not neighbouring context")
        require(0 <= span["start"] < span["end"] <= len(row["text"]), "Evidence offset outside paragraph")
        require(row["text"][span["start"]:span["end"]] == span["quote"], "Evidence quote does not match exact code-point span")
        key = [span["paragraph_id"], span["start"], span["end"]]
        eid = identifier("ev", key)
        start, end = row["char_start"] + span["start"], row["char_start"] + span["end"]
        con.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?,?,?)",
                    (eid, span["paragraph_id"], span["start"], span["end"], span["quote"], start, end))
        return eid, start, end

    def submit(self, task_id, token, payload, context_support=None, entity_preflight=None):
        validate(payload)
        stored = payload if context_support is None else {"payload": payload, "context_support": context_support}
        if entity_preflight is not None:
            stored = {"payload": payload, "context_support": context_support or [], "entity_preflight": entity_preflight}
        body = encoded(stored)
        require(len(body.encode("utf-8")) <= 8_000_000, "Submit smaller batches (8 MB maximum)")
        body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self.connection(True) as con:
            old = con.execute("SELECT payload_sha256,receipt_json FROM submissions WHERE task_id=? AND lease_token=? AND batch_id=?",
                              (task_id, token, payload["batch_id"])).fetchone()
            if old:
                require(old[0] == body_sha, "Batch ID already committed with a different payload")
                return {**json.loads(old[1]), "idempotent_replay": True}
            policy = con.execute("SELECT value FROM metadata WHERE key='entity_preflight_policy'").fetchone()
            if policy and any(e["existing_id"] is None for e in payload["entries"]):
                from entity_preflight import VERSION as LOOKUP_VERSION, verify_proof
                require(policy[0] == LOOKUP_VERSION, "Unsupported global entity lookup policy")
                verify_proof(self, payload, entity_preflight, con)
            representation = require_valid(payload)
            now = time.time()
            task = self._lease(con, task_id, token, now)
            sid = identifier("sub", [task_id, token, payload["batch_id"]])
            entry_refs = [e["ref"] for e in payload["entries"]]
            fact_refs = [f["ref"] for f in payload["facts"]]
            require(len(set(entry_refs)) == len(entry_refs), "Duplicate entry ref")
            require(len(set(fact_refs)) == len(fact_refs), "Duplicate fact ref")
            ids, fact_ids = {}, {}
            possible_duplicates = []

            def provenance(kind, rid):
                con.execute("INSERT OR IGNORE INTO provenance VALUES (?,?,?)", (sid, kind, rid))

            for entry in payload["entries"]:
                spans = [self._span(con, task, s) for s in entry["evidence"]]
                if entry["existing_id"]:
                    existing = con.execute("SELECT * FROM entities WHERE entity_id=?", (entry["existing_id"],)).fetchone()
                    require(existing is not None, "Unknown existing entity ID")
                    require(existing["kind"] == entry["kind"], "Existing entity kind differs; record a correction instead of silently changing it")
                    known_names = {normalize(existing["label"])} | {normalize(r["surface"]) for r in self.entity_names(con, entry["existing_id"])}
                    require(normalize(entry["name"]) in known_names, "Existing entity ID/name mismatch; use its registered name and record a new alias as a mention or identity candidate")
                    eid = entry["existing_id"]
                else:
                    eid = identifier("ent", [entry["kind"], normalize(entry["name"]), sorted(s[0] for s in spans)])
                con.execute("INSERT OR IGNORE INTO entities VALUES (?,?,?,?,?,?)",
                                (eid, entry["kind"], entry["name"], "candidate", encoded({"task_id": task_id}), now))
                require(con.execute("SELECT 1 FROM record_decisions WHERE record_type='entity' AND record_id=? AND state='rejected'", (eid,)).fetchone() is None,
                        "Entity is rejected; preserve a separate supported candidate instead of reusing it")
                # Recheck inside the same transaction. Same name is a candidate
                # collision, never a uniqueness constraint or automatic merge.
                others = [r[0] for r in con.execute("SELECT DISTINCT n.entity_id FROM names n JOIN entities e USING(entity_id) WHERE n.normalized=? AND e.kind=? AND n.entity_id<>?",
                                                     (normalize(entry["name"]), entry["kind"], eid))]
                if others:
                    possible_duplicates.append({"entity_id": eid, "name": entry["name"], "candidate_ids": others})
                if not entry["existing_id"]:
                    self._name(con, eid, entry["name"], "observed_label_candidate", task["unit_id"], task["unit_id"], "extraction")
                for ev, _, _ in spans:
                    con.execute("INSERT OR IGNORE INTO entity_evidence VALUES (?,?)", (eid, ev))
                ids[entry["ref"]] = eid
                provenance("entity", eid)

            def reference(ref):
                require(ref in ids, f"Undeclared local entry reference: {ref}")
                return ids[ref]

            for mention in payload["mentions"]:
                eid = reference(mention["entity"])
                ev, _, _ = self._span(con, task, mention["span"])
                mid = identifier("mention", [eid, ev])
                con.execute("INSERT OR IGNORE INTO mentions VALUES (?,?,?,?)", (mid, eid, ev, mention["resolution_note"]))
                provenance("mention", mid)
            for fact in payload["facts"]:
                subject = reference(fact["subject"])
                target = reference(fact["object"]) if fact["object"] is not None else None
                speaker = reference(fact["speaker"]) if fact["speaker"] is not None else None
                require((target is None) != (fact["value"] is None), "Fact needs exactly one object or literal value")
                if fact["type"] == "attribute":
                    require(target is None, "Attribute uses a literal value")
                elif fact["type"] in ("relation", "identity"):
                    require(target is not None, "Relation and identity facts need an object")
                if fact["type"] == "temporal":
                    ordering = ("before", "after", "simultaneous", "overlaps", "unknown")
                    require(fact["predicate"] in (*ordering, "occurs_at"), "Unknown temporal predicate")
                    require((target is None) == (fact["predicate"] == "occurs_at"),
                            "Temporal occurs_at uses a literal time; temporal order needs an object")
                    require(fact["predicate"] not in ("before", "after") or subject != target,
                            "An event cannot be strictly before or after itself")
                    kinds = [con.execute("SELECT kind FROM entities WHERE entity_id=?", (x,)).fetchone()[0] for x in (subject, target) if x is not None]
                    require(all(k in ("event", "scene", "utterance", "state") for k in kinds), "Temporal order connects events, scenes, utterances or states")
                if fact["type"] == "identity":
                    require(fact["predicate"] in ("same_as", "different_from"), "Unknown identity predicate")
                    require(subject != target, "Identity comparison requires two distinct entity IDs")
                spans = [self._span(con, task, s) for s in fact["evidence"]]
                content = {k: v for k, v in fact.items() if k not in ("ref", "evidence", "subject", "object", "speaker")}
                content.update(subject=subject, object=target, speaker=speaker, evidence=sorted(s[0] for s in spans))
                fid = identifier("fact", content)
                con.execute("INSERT OR IGNORE INTO facts(fact_id,fact_type,subject_id,predicate,object_id,value_json,polarity,modality,attribution,speaker_id,frame,story_time,note,narrative_start,narrative_end) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (fid, fact["type"], subject, fact["predicate"], target,
                             encoded(fact["value"]) if fact["value"] is not None else None,
                             fact["polarity"], fact["modality"], fact["attribution"], speaker, fact["frame"],
                             fact["story_time"], fact["note"], min(s[1] for s in spans), max(s[2] for s in spans)))
                for ev, _, _ in spans:
                    con.execute("INSERT OR IGNORE INTO fact_evidence VALUES (?,?)", (fid, ev))
                provenance("fact", fid)
                fact_ids[fact["ref"]] = fid

            for support in context_support or []:
                require(set(support) == {"record_type", "ref", "evidence", "note"} and support["record_type"] in ("entity", "fact") and support["evidence"] and support["note"].strip(), "Invalid context support")
                targets = ids if support["record_type"] == "entity" else fact_ids
                require(support["ref"] in targets, "Context support references an undeclared record")
                for span in support["evidence"]:
                    ev, _, _ = self._span(con, task, span, body_only=False)
                    con.execute("INSERT OR IGNORE INTO context_support VALUES (?,?,?,?,?)", (sid, support["record_type"], targets[support["ref"]], ev, support["note"]))

            body_ids = {r[0] for r in con.execute("SELECT paragraph_id FROM paragraphs WHERE unit_id=? AND ordinal BETWEEN ? AND ?",
                                                 (task["unit_id"], task["first_paragraph"], task["last_paragraph"]))}
            require(set(payload["coverage"]) <= body_ids, "Coverage outside task body")
            for pid in set(payload["coverage"]):
                con.execute("INSERT INTO coverage VALUES (?,?,?)", (task_id, pid, sid))
            for issue in payload["unresolved"]:
                require(issue["paragraph_id"] in body_ids, "Unresolved issue outside body")
                con.execute("INSERT OR IGNORE INTO unresolved VALUES (?,?,?,?,?)",
                            (identifier("issue", [task_id, issue]), task_id, issue["paragraph_id"], issue["description"], sid))
            covered = {r[0] for r in con.execute("SELECT DISTINCT paragraph_id FROM coverage WHERE task_id=?", (task_id,))}
            if payload["complete"]:
                require(covered == body_ids, f"Cannot complete: {len(body_ids - covered)} paragraphs not declared inspected")
                con.execute("UPDATE tasks SET state='complete',completed_at=?,lease_until=NULL WHERE task_id=?", (now, task_id))
                con.execute("UPDATE attempts SET outcome='complete',ended_at=? WHERE lease_token=?", (now, token))
            receipt = {"submission_id": sid, "task_id": task_id, "state": "complete" if payload["complete"] else "leased",
                       "entity_ids": ids, "fact_ids": fact_ids, "possible_duplicates": possible_duplicates,
                       "covered_paragraphs": len(covered), "total_paragraphs": len(body_ids),
                       "evidence_validation": "exact spans passed", "semantic_review": "pending",
                       "context_support_records": len(context_support or []),
                       "representation_checks": representation,
                       "idempotent_replay": False}
            con.execute("INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?)",
                        (sid, task_id, token, payload["batch_id"], body_sha, body, encoded(receipt), now))
            return receipt

    def lookup(self, name, kind=None, limit=20, offset=0):
        require(1 <= limit <= 100 and offset >= 0 and bool(name.strip()), "Invalid lookup parameters")
        with self.connection() as con:
            escaped = normalize(name).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params = (normalize(name), "%" + escaped + "%", kind, kind)
            where = "(n.normalized=? OR n.normalized LIKE ? ESCAPE '\\') AND (? IS NULL OR e.kind=?)"
            where += " AND " + self.visible("entity", "e.entity_id")
            prefix = "WITH " + self.name_surfaces() + " "
            total = con.execute(prefix + "SELECT count(DISTINCT e.entity_id) FROM entities e JOIN name_surfaces n USING(entity_id) WHERE " + where, params).fetchone()[0]
            rows = [dict(r) for r in con.execute(prefix + "SELECT e.*,max(n.normalized=?) AS exact_name_match FROM entities e JOIN name_surfaces n USING(entity_id) WHERE " + where + " GROUP BY e.entity_id ORDER BY exact_name_match DESC,e.entity_id LIMIT ? OFFSET ?", (normalize(name),) + params + (limit, offset))]
            for row in rows:
                row["origin"] = json.loads(row.pop("origin_json"))
                row["names"] = self.entity_names(con, row["entity_id"])
            return {"total": total, "offset": offset, "items": rows, "has_more": offset + len(rows) < total,
                    "identity_warning": "A name match is a candidate, not proof of identity"}

    def timeline(self, entity_id=None, limit=100, offset=0, as_of=None, review_state="active"):
        require(1 <= limit <= 1000 and offset >= 0, "Invalid pagination")
        require(review_state in ("active", "accepted", "pending", "rejected", "all"), "Invalid review filter")
        with self.connection() as con:
            params = (entity_id, entity_id, entity_id, as_of, as_of, review_state, review_state, review_state)
            where = "(? IS NULL OR subject_id=? OR object_id=?) AND (? IS NULL OR narrative_end<=?) AND (?='all' OR (?='active' AND review_state<>'rejected') OR review_state=?)"
            if review_state not in ("all", "rejected"):
                for column in ("subject_id", "object_id", "speaker_id"):
                    where += " AND " + self.visible("entity", "narrative_facts." + column)
            total = con.execute("SELECT count(*) FROM narrative_facts WHERE " + where, params).fetchone()[0]
            rows = [dict(r) for r in con.execute("SELECT * FROM narrative_facts WHERE " + where + " ORDER BY narrative_start,narrative_end,fact_id LIMIT ? OFFSET ?", params + (limit, offset))]
            for row in rows:
                literal = row.pop("value_json")
                row["value"] = json.loads(literal) if literal is not None else None
                row["evidence"] = [dict(r) for r in con.execute("SELECT e.*,p.text AS paragraph_text FROM evidence e JOIN fact_evidence f USING(evidence_id) JOIN paragraphs p USING(paragraph_id) WHERE f.fact_id=? ORDER BY global_start,global_end", (row["fact_id"],))]
                row["context_support"] = [dict(r) for r in con.execute("SELECT e.*,s.note FROM context_support s JOIN evidence e USING(evidence_id) WHERE s.record_type='fact' AND s.record_id=? ORDER BY e.global_start,e.global_end", (row["fact_id"],))]
            return {"axis": "source evidence order; not story chronology", "review_filter": review_state,
                    "semantic_warning": "Pending facts are candidates, not established truth. as_of filters evidence, not all identity/name hindsight.", "total": total, "offset": offset,
                    "items": rows, "has_more": offset + len(rows) < total}

    def entry(self, entity_id, limit=100, offset=0, review_state="active"):
        with self.connection() as con:
            row = con.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
            require(row is not None, "Unknown entity")
            decision = con.execute("SELECT * FROM record_decisions WHERE record_type='entity' AND record_id=?", (entity_id,)).fetchone()
            require(review_state == "all" or not decision or decision["state"] != "rejected", "Entity is rejected; use review_state=all only for audit")
            value = dict(row)
            value["record_review"] = dict(decision) if decision else {"state": "pending", "revision": 0}
            value["context_support"] = [dict(r) for r in con.execute("SELECT e.*,s.note FROM context_support s JOIN evidence e USING(evidence_id) WHERE s.record_type='entity' AND s.record_id=? ORDER BY e.global_start,e.global_end", (entity_id,))]
            mention_filter = "" if review_state == "all" else " AND " + self.visible("mention", "mentions.mention_id")
            value["names"] = self.entity_names(con, entity_id, review_state == "all")
            value["mention_count"] = con.execute("SELECT count(*) FROM mentions WHERE entity_id=?" + mention_filter, (entity_id,)).fetchone()[0]
        value["facts"] = self.timeline(entity_id, limit, offset, review_state=review_state)
        return value

    def review(self, fact_id, state, reviewer, reason, expected_revision):
        require(state in ("pending", "accepted", "rejected") and reviewer.strip() and reason.strip(), "Invalid review")
        with self.connection(True) as con:
            row = con.execute("SELECT review_revision FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            require(row is not None and row[0] == expected_revision, "Review revision changed; reread before updating")
            revision = row[0] + 1
            con.execute("UPDATE facts SET review_state=?,review_revision=? WHERE fact_id=?", (state, revision, fact_id))
            con.execute("INSERT INTO reviews(fact_id,revision,state,reviewer,reason,created_at) VALUES (?,?,?,?,?,?)",
                        (fact_id, revision, state, reviewer, reason, time.time()))
            return {"fact_id": fact_id, "review_state": state, "revision": revision,
                    "note": "Identity review does not destructively merge entity IDs"}

    def review_record(self, record_type, record_id, state, reviewer, reason, expected_revision):
        require(record_type in ("entity", "name", "mention") and state in ("pending", "accepted", "rejected") and reviewer.strip() and reason.strip(), "Invalid record review")
        table, key = {"entity": ("entities", "entity_id"), "name": ("names", "name_id"), "mention": ("mentions", "mention_id")}[record_type]
        with self.connection(True) as con:
            require(con.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (record_id,)).fetchone() is not None, "Unknown review record")
            row = con.execute("SELECT revision FROM record_decisions WHERE record_type=? AND record_id=?", (record_type, record_id)).fetchone()
            require((row[0] if row else 0) == expected_revision, "Review revision changed; reread before updating")
            revision = expected_revision + 1
            con.execute("INSERT OR REPLACE INTO record_decisions VALUES (?,?,?,?)", (record_type, record_id, state, revision))
            con.execute("INSERT INTO record_review_log VALUES (?,?,?,?,?,?,?)", (record_type, record_id, revision, state, reviewer, reason, time.time()))
            return dict(record_type=record_type, record_id=record_id, state=state, revision=revision)

    def status(self):
        with self.connection() as con:
            return {"database": str(self.path), "metadata": dict(con.execute("SELECT key,value FROM metadata")),
                    "journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0],
                    "counts": {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                               for t in ("units", "paragraphs", "entities", "names", "mentions", "facts", "submissions", "unresolved")},
                    "tasks": dict(con.execute("SELECT state,count(*) FROM tasks GROUP BY state")),
                    "semantic_review": dict(con.execute("SELECT review_state,count(*) FROM facts GROUP BY review_state"))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    listing = sub.add_parser("tasks")
    listing.add_argument("--state")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--offset", type=int, default=0)
    retry = sub.add_parser("retry")
    retry.add_argument("--task", required=True)
    retry.add_argument("--reason", required=True)
    retry.add_argument("--additional-attempts", type=int, default=1)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--unit", action="append")
    enqueue.add_argument("--pass-name", default="extract-v1")
    enqueue.add_argument("--first", type=int)
    enqueue.add_argument("--last", type=int)
    claim = sub.add_parser("claim")
    claim.add_argument("--worker", required=True)
    claim.add_argument("--count", type=int, default=1)
    claim.add_argument("--seconds", type=int, default=1800)
    claim.add_argument("--pass-name")
    for command in ("task", "heartbeat", "release", "yield", "submit", "prepare"):
        item = sub.add_parser(command)
        item.add_argument("--task", required=True)
        item.add_argument("--lease", required=True)
        if command == "heartbeat":
            item.add_argument("--seconds", type=int, default=1800)
        if command in ("release", "yield"):
            item.add_argument("--reason", required=True)
        if command in ("submit", "prepare"):
            item.add_argument("--file", type=Path, required=True)
        if command == "submit":
            item.add_argument("--identity-proof", type=Path)
            item.add_argument("--context-support", type=Path)
        if command == "prepare":
            item.add_argument("--output", type=Path, required=True)
    locate = sub.add_parser("locate")
    locate.add_argument("--task", required=True)
    locate.add_argument("--lease", required=True)
    locate.add_argument("--file", type=Path, required=True, help="UTF-8 JSON with quote and optional paragraph_id")
    lookup = sub.add_parser("lookup")
    lookup.add_argument("--name", required=True)
    lookup.add_argument("--kind")
    for command in ("entry", "timeline"):
        item = sub.add_parser(command)
        item.add_argument("--entity", required=command == "entry")
        item.add_argument("--review-state", choices=("active", "accepted", "pending", "rejected", "all"), default="active")
        if command == "timeline":
            item.add_argument("--as-of", type=int)
    for command in (lookup, sub.choices["entry"], sub.choices["timeline"]):
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--offset", type=int, default=0)
    review = sub.add_parser("review")
    review.add_argument("--fact", required=True)
    review.add_argument("--state", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--expected-revision", type=int, required=True)
    args = parser.parse_args()
    reg = Registry(args.db)
    try:
        if args.command == "status": result = reg.status()
        elif args.command == "tasks": result = reg.tasks(args.state, args.limit, args.offset)
        elif args.command == "retry": result = reg.retry(args.task, args.reason, args.additional_attempts)
        elif args.command == "enqueue": result = reg.enqueue(args.unit, args.pass_name, args.first, args.last)
        elif args.command == "claim": result = reg.claim(args.worker, args.count, args.seconds, args.pass_name)
        elif args.command == "task": result = reg.task(args.task, args.lease)
        elif args.command == "heartbeat": result = reg.heartbeat(args.task, args.lease, args.seconds)
        elif args.command == "release": result = reg.release(args.task, args.lease, args.reason)
        elif args.command == "yield": result = reg.yield_task(args.task, args.lease, args.reason)
        elif args.command == "prepare":
            from prepare_submission import prepare
            payload = prepare(reg, args.task, args.lease, json.loads(args.file.read_text(encoding="utf-8-sig")))
            destination = local_path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("x", encoding="utf-8") as out:
                json.dump(payload, out, ensure_ascii=False, indent=2)
            result = {"prepared_file": str(destination), "submitted": False,
                      "note": "Exact quote positions resolved; semantic correctness has not been established"}
        elif args.command == "submit":
            result = reg.submit(args.task, args.lease, json.loads(args.file.read_text(encoding="utf-8-sig")),
                context_support=json.loads(args.context_support.read_text(encoding="utf-8-sig")) if args.context_support else None,
                entity_preflight=json.loads(args.identity_proof.read_text(encoding="utf-8-sig")) if args.identity_proof else None)
        elif args.command == "locate":
            query = json.loads(args.file.read_text(encoding="utf-8-sig"))
            result = reg.locate(args.task, args.lease, query["quote"], query.get("paragraph_id"))
        elif args.command == "lookup": result = reg.lookup(args.name, args.kind, args.limit, args.offset)
        elif args.command == "entry": result = reg.entry(args.entity, args.limit, args.offset, args.review_state)
        elif args.command == "timeline": result = reg.timeline(args.entity, args.limit, args.offset, args.as_of, args.review_state)
        else: result = reg.review(args.fact, args.state, args.reviewer, args.reason, args.expected_revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error, OSError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
