CREATE TABLE IF NOT EXISTS record_decisions(
  record_type TEXT NOT NULL CHECK(record_type IN ('entity','name','mention')),
  record_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')),
  revision INTEGER NOT NULL,
  PRIMARY KEY(record_type,record_id)
);
CREATE TABLE IF NOT EXISTS record_review_log(
  record_type TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
  state TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(record_type,record_id,revision)
);
CREATE TABLE IF NOT EXISTS context_support(
  submission_id TEXT NOT NULL REFERENCES submissions(submission_id) DEFERRABLE INITIALLY DEFERRED,
  record_type TEXT NOT NULL CHECK(record_type IN ('entity','fact')),
  record_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
  note TEXT NOT NULL,
  PRIMARY KEY(submission_id,record_type,record_id,evidence_id,note)
);
CREATE INDEX IF NOT EXISTS context_support_record ON context_support(record_type,record_id);
