PRAGMA foreign_keys = ON;
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = FULL;

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE source_files (
    path TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    record_count INTEGER CHECK (record_count IS NULL OR record_count >= 0)
) WITHOUT ROWID;

CREATE TABLE chapters (
    chapter_id TEXT PRIMARY KEY,
    chapter_number INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    volume_title TEXT,
    path TEXT NOT NULL UNIQUE,
    char_count INTEGER NOT NULL CHECK (char_count >= 0),
    paragraph_count INTEGER NOT NULL CHECK (paragraph_count >= 0)
) WITHOUT ROWID;

CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    chunk_index INTEGER NOT NULL CHECK (chunk_index > 0),
    path TEXT NOT NULL UNIQUE,
    text_sha256 TEXT NOT NULL CHECK (length(text_sha256) = 64),
    text TEXT NOT NULL,
    UNIQUE (chapter_id, chunk_index)
) WITHOUT ROWID;

CREATE INDEX chunks_chapter_idx ON chunks(chapter_id, chunk_index);

CREATE TABLE chapter_summaries (
    chapter_id TEXT PRIMARY KEY REFERENCES chapters(chapter_id),
    summary TEXT NOT NULL,
    key_points_json TEXT NOT NULL CHECK (json_valid(key_points_json)),
    evidence_refs_json TEXT NOT NULL CHECK (json_valid(evidence_refs_json)),
    source_path TEXT NOT NULL
) WITHOUT ROWID;

CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('character', 'faction', 'item', 'location')),
    source_name TEXT NOT NULL,
    english_name TEXT,
    naming_status TEXT,
    summary TEXT NOT NULL,
    first_seen_chapter_id TEXT REFERENCES chapters(chapter_id),
    last_seen_chapter_id TEXT REFERENCES chapters(chapter_id),
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE INDEX entities_type_idx ON entities(entity_type, entity_id);
CREATE INDEX entities_source_name_idx ON entities(source_name);

CREATE TABLE entity_aliases (
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    alias TEXT NOT NULL,
    alias_kind TEXT NOT NULL CHECK (alias_kind IN ('source_alias', 'english_name')),
    PRIMARY KEY (entity_id, alias, alias_kind)
) WITHOUT ROWID;

CREATE INDEX entity_alias_surface_idx ON entity_aliases(alias, entity_id);

CREATE TABLE entity_tags (
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (entity_id, tag)
) WITHOUT ROWID;

CREATE TABLE entity_evidence (
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    PRIMARY KEY (entity_id, chapter_id, chunk_id)
) WITHOUT ROWID;

-- A literary identity decision may supersede an older automatic mention
-- binding without rewriting the historical scan.  These rows are the small,
-- explicit bridge between the frozen mention ledger and newly curated
-- identity facets/components.
CREATE TABLE endpoint_resolutions (
    resolution_id TEXT PRIMARY KEY,
    previous_mention_id TEXT,
    resolved_mention_id TEXT NOT NULL UNIQUE,
    previous_entity_id TEXT REFERENCES entities(entity_id),
    resolved_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    quote_exact TEXT NOT NULL,
    chunk_text_sha256 TEXT NOT NULL CHECK (length(chunk_text_sha256) = 64),
    source_path TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status = 'accepted'),
    review_state TEXT NOT NULL CHECK (review_state = 'approved'),
    reviewer_kind TEXT NOT NULL,
    approver_activity_ref TEXT NOT NULL,
    review_rationale TEXT NOT NULL,
    created_by_activity_ref TEXT NOT NULL,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE INDEX endpoint_resolution_chunk_idx
    ON endpoint_resolutions(chunk_id, start_offset, end_offset);

CREATE TABLE naming_decisions (
    entity_id TEXT PRIMARY KEY REFERENCES entities(entity_id),
    canonical_english_name TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    decision_batch_id TEXT NOT NULL,
    decision_note TEXT,
    policy_version TEXT,
    lifecycle_status TEXT,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE TABLE naming_candidates (
    candidate_id TEXT PRIMARY KEY,
    current_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    current_english_name TEXT NOT NULL,
    proposed_entity_id TEXT NOT NULL,
    proposed_english_name TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE TABLE relation_occurrences (
    occurrence_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    predicate TEXT NOT NULL,
    object_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    polarity TEXT NOT NULL,
    modality TEXT NOT NULL,
    certainty TEXT NOT NULL,
    assertion_scope TEXT NOT NULL,
    evidence_class TEXT NOT NULL,
    perspective_json TEXT NOT NULL CHECK (json_valid(perspective_json)),
    temporal_context_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    quote_exact TEXT NOT NULL,
    chunk_text_sha256 TEXT NOT NULL CHECK (length(chunk_text_sha256) = 64),
    source_path TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status = 'accepted'),
    review_state TEXT NOT NULL CHECK (review_state = 'approved'),
    approver_activity_ref TEXT NOT NULL,
    review_rationale TEXT NOT NULL,
    created_by_activity_ref TEXT NOT NULL,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE INDEX relation_occurrence_pair_idx
    ON relation_occurrences(subject_entity_id, predicate, object_entity_id);
CREATE INDEX relation_occurrence_chunk_idx ON relation_occurrences(chunk_id, start_offset);

CREATE TABLE relation_occurrence_qualifiers (
    occurrence_id TEXT NOT NULL REFERENCES relation_occurrences(occurrence_id) ON DELETE CASCADE,
    qualifier_key TEXT NOT NULL,
    qualifier_value TEXT NOT NULL,
    PRIMARY KEY (occurrence_id, qualifier_key, qualifier_value)
) WITHOUT ROWID;

CREATE TABLE relation_endpoint_bindings (
    occurrence_id TEXT NOT NULL REFERENCES relation_occurrences(occurrence_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('subject', 'object')),
    binding_kind TEXT NOT NULL CHECK (binding_kind IN ('mention', 'identity_resolution')),
    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    mention_id TEXT,
    resolution_id TEXT REFERENCES endpoint_resolutions(resolution_id),
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json)),
    PRIMARY KEY (occurrence_id, role),
    CHECK (
        (binding_kind = 'mention' AND mention_id IS NOT NULL AND resolution_id IS NULL)
        OR
        (binding_kind = 'identity_resolution' AND mention_id IS NOT NULL AND resolution_id IS NOT NULL)
    )
) WITHOUT ROWID;

CREATE TABLE relation_episodes (
    relation_episode_id TEXT PRIMARY KEY,
    relation_key TEXT NOT NULL,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    predicate TEXT NOT NULL,
    object_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    polarity TEXT NOT NULL,
    story_time_json TEXT NOT NULL CHECK (json_valid(story_time_json)),
    state_at_close TEXT NOT NULL,
    evidence_class TEXT NOT NULL,
    summary TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status = 'accepted'),
    review_state TEXT NOT NULL CHECK (review_state = 'approved'),
    approver_activity_ref TEXT NOT NULL,
    review_rationale TEXT NOT NULL,
    created_by_activity_ref TEXT NOT NULL,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE INDEX relation_episode_pair_idx
    ON relation_episodes(subject_entity_id, predicate, object_entity_id);
CREATE INDEX relation_episode_key_idx ON relation_episodes(relation_key, relation_episode_id);

CREATE TABLE relation_episode_qualifiers (
    relation_episode_id TEXT NOT NULL REFERENCES relation_episodes(relation_episode_id) ON DELETE CASCADE,
    qualifier_key TEXT NOT NULL,
    qualifier_value TEXT NOT NULL,
    PRIMARY KEY (relation_episode_id, qualifier_key, qualifier_value)
) WITHOUT ROWID;

CREATE TABLE relation_episode_support (
    relation_episode_id TEXT NOT NULL REFERENCES relation_episodes(relation_episode_id) ON DELETE CASCADE,
    occurrence_id TEXT NOT NULL REFERENCES relation_occurrences(occurrence_id),
    support_kind TEXT NOT NULL CHECK (support_kind IN ('support', 'challenge')),
    PRIMARY KEY (relation_episode_id, occurrence_id, support_kind)
) WITHOUT ROWID;

CREATE TABLE relation_candidates (
    candidate_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (review_status <> 'accepted'),
    episode_policy TEXT NOT NULL,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    predicate TEXT NOT NULL,
    object_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    certainty TEXT NOT NULL,
    assertion_scope TEXT NOT NULL,
    proposed_state_at_close TEXT,
    chapter_id TEXT NOT NULL REFERENCES chapters(chapter_id),
    chunk_id TEXT NOT NULL REFERENCES chunks(chunk_id),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    quote_exact TEXT NOT NULL,
    chunk_text_sha256 TEXT NOT NULL CHECK (length(chunk_text_sha256) = 64),
    confidence REAL,
    review_rationale TEXT NOT NULL,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json))
) WITHOUT ROWID;

CREATE INDEX relation_candidate_entity_idx
    ON relation_candidates(subject_entity_id, object_entity_id, candidate_id);

CREATE TABLE relation_candidate_qualifiers (
    candidate_id TEXT NOT NULL REFERENCES relation_candidates(candidate_id) ON DELETE CASCADE,
    qualifier_key TEXT NOT NULL,
    qualifier_value TEXT NOT NULL,
    PRIMARY KEY (candidate_id, qualifier_key, qualifier_value)
) WITHOUT ROWID;

CREATE VIEW accepted_relations AS
SELECT
    relation_episode_id,
    relation_key,
    subject_entity_id,
    predicate,
    object_entity_id,
    state_at_close,
    summary
FROM relation_episodes
WHERE lifecycle_status = 'accepted' AND review_state = 'approved';

CREATE VIEW accepted_relation_evidence AS
SELECT
    e.relation_episode_id,
    e.relation_key,
    s.support_kind,
    o.occurrence_id,
    o.chapter_id,
    o.chunk_id,
    o.start_offset,
    o.end_offset,
    o.quote_exact,
    o.source_path
FROM relation_episodes AS e
JOIN relation_episode_support AS s
  ON s.relation_episode_id = e.relation_episode_id
JOIN relation_occurrences AS o
  ON o.occurrence_id = s.occurrence_id
WHERE e.lifecycle_status = 'accepted' AND e.review_state = 'approved';

CREATE VIEW entity_neighbors AS
SELECT
    subject_entity_id AS entity_id,
    object_entity_id AS neighbor_entity_id,
    predicate,
    'outgoing' AS direction,
    relation_episode_id,
    state_at_close,
    summary
FROM accepted_relations
UNION ALL
SELECT
    object_entity_id AS entity_id,
    subject_entity_id AS neighbor_entity_id,
    predicate,
    'incoming' AS direction,
    relation_episode_id,
    state_at_close,
    summary
FROM accepted_relations;
