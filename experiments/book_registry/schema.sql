PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = FULL;

CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE units(
  unit_id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL UNIQUE,
  heading TEXT NOT NULL, declared_start INTEGER, declared_end INTEGER,
  heading_kind TEXT NOT NULL
);
CREATE TABLE paragraphs(
  paragraph_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES units(unit_id),
  ordinal INTEGER NOT NULL, source_line INTEGER NOT NULL, snapshot_line INTEGER NOT NULL,
  char_start INTEGER NOT NULL UNIQUE, text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
  UNIQUE(unit_id, ordinal)
);
CREATE TABLE tasks(
  task_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES units(unit_id),
  first_paragraph INTEGER NOT NULL, last_paragraph INTEGER NOT NULL,
  pass_name TEXT NOT NULL, spec_version TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','complete','failed')),
  attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 3,
  owner TEXT, lease_token TEXT, lease_until REAL, last_error TEXT,
  created_at REAL NOT NULL, completed_at REAL,
  UNIQUE(unit_id, first_paragraph, last_paragraph, pass_name, spec_version)
);
CREATE INDEX tasks_claim ON tasks(state, lease_until, unit_id);
CREATE TABLE attempts(
  lease_token TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
  worker TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
  outcome TEXT NOT NULL CHECK(outcome IN ('running','expired','released','complete','failed'))
);
CREATE TABLE submissions(
  submission_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
  lease_token TEXT NOT NULL REFERENCES attempts(lease_token), batch_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
  receipt_json TEXT NOT NULL, created_at REAL NOT NULL,
  UNIQUE(task_id, lease_token, batch_id)
);
CREATE TABLE entities(
  entity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('seed_unverified','candidate')),
  origin_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE names(
  name_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(entity_id),
  surface TEXT NOT NULL, normalized TEXT NOT NULL, relation_type TEXT NOT NULL,
  valid_from_unit TEXT, valid_to_unit TEXT, origin TEXT NOT NULL
);
CREATE INDEX names_lookup ON names(normalized, entity_id);
CREATE INDEX names_entity ON names(entity_id);
CREATE TABLE evidence(
  evidence_id TEXT PRIMARY KEY, paragraph_id TEXT NOT NULL REFERENCES paragraphs(paragraph_id),
  start INTEGER NOT NULL, end INTEGER NOT NULL, quote TEXT NOT NULL,
  global_start INTEGER NOT NULL, global_end INTEGER NOT NULL,
  CHECK(start >= 0 AND end > start),
  UNIQUE(paragraph_id,start,end)
);
CREATE TABLE entity_evidence(
  entity_id TEXT NOT NULL REFERENCES entities(entity_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(entity_id,evidence_id)
);
CREATE TABLE mentions(
  mention_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(entity_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  resolution_note TEXT NOT NULL,
  UNIQUE(entity_id,evidence_id)
);
CREATE TABLE facts(
  fact_id TEXT PRIMARY KEY,
  fact_type TEXT NOT NULL CHECK(fact_type IN ('relation','attribute','temporal','identity')),
  subject_id TEXT NOT NULL REFERENCES entities(entity_id), predicate TEXT NOT NULL,
  object_id TEXT REFERENCES entities(entity_id), value_json TEXT,
  polarity TEXT NOT NULL CHECK(polarity IN ('positive','negative')),
  modality TEXT NOT NULL CHECK(modality IN ('actual','planned','reported','conditional','hypothetical','unknown')),
  attribution TEXT NOT NULL CHECK(attribution IN ('narrator','character','inference','unknown')),
  speaker_id TEXT REFERENCES entities(entity_id),
  frame TEXT NOT NULL, story_time TEXT, note TEXT NOT NULL,
  narrative_start INTEGER NOT NULL, narrative_end INTEGER NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'pending' CHECK(review_state IN ('pending','accepted','rejected')),
  review_revision INTEGER NOT NULL DEFAULT 0,
  CHECK((object_id IS NOT NULL) != (value_json IS NOT NULL))
);
CREATE INDEX facts_subject ON facts(subject_id,narrative_start,fact_id);
CREATE INDEX facts_object ON facts(object_id,narrative_start,fact_id);
CREATE INDEX facts_narrative ON facts(narrative_start,narrative_end,fact_id);
CREATE TABLE fact_evidence(
  fact_id TEXT NOT NULL REFERENCES facts(fact_id),
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  PRIMARY KEY(fact_id,evidence_id)
);
CREATE TABLE provenance(
  submission_id TEXT NOT NULL REFERENCES submissions(submission_id) DEFERRABLE INITIALLY DEFERRED,
  record_type TEXT NOT NULL CHECK(record_type IN ('entity','mention','fact')),
  record_id TEXT NOT NULL,
  PRIMARY KEY(submission_id,record_type,record_id)
);
CREATE TABLE coverage(
  task_id TEXT NOT NULL REFERENCES tasks(task_id),
  paragraph_id TEXT NOT NULL REFERENCES paragraphs(paragraph_id),
  submission_id TEXT NOT NULL REFERENCES submissions(submission_id) DEFERRABLE INITIALLY DEFERRED,
  PRIMARY KEY(task_id,paragraph_id,submission_id)
);
CREATE TABLE unresolved(
  issue_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
  paragraph_id TEXT NOT NULL REFERENCES paragraphs(paragraph_id),
  description TEXT NOT NULL,
  submission_id TEXT NOT NULL REFERENCES submissions(submission_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE reviews(
  review_id INTEGER PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(fact_id),
  revision INTEGER NOT NULL, state TEXT NOT NULL, reviewer TEXT NOT NULL,
  reason TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(fact_id,revision)
);
CREATE VIEW narrative_facts AS
  SELECT f.*, m.value || ':' || printf('%010d',f.narrative_start) || ':' ||
    printf('%010d',f.narrative_end) || ':' || f.fact_id AS narrative_key
  FROM facts f JOIN metadata m ON m.key='source_sha256';
