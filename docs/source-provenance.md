# Source provenance and portability

The maintained compiler and SQL schema were extracted from a long-form Chinese literary curation project. The compiler was not replaced with a simplified graph demo. The release adapter changes the fixed chapter/chunk/naming inventory and focus entity into a declared dataset contract; the supplied test corpus is separate from the original literary records.

Retained behavior includes strict JSON, deterministic identifiers, exact evidence slices, independent-activity checks, explicit endpoint resolutions, accepted/pending isolation, refinement lineage, gold-case consistency, transactional SQLite construction, source inventory, drift detection and generated reports.

The original integration tests depend on a complete privately held novel and its curation corpus. Public tests exercise the adapted compiler on independently constructed inputs; they do not claim identical whole-corpus coverage. The broader original alignment workflow is not part of this compiler release.

A private mapping retains exact source and release hashes. Public commit timestamps reflect publication, not the start date of the original work.

Fixture IDs such as `fixture:producer` and `fixture:reviewer` are test identities. Fields representing approval exercise the storage contract; they are not claims that a human or a separate model actually approved the fixture text. Real data owners must supply genuine review evidence.
