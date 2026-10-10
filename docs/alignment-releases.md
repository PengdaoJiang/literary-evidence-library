# Immutable alignment releases

The alignment subsystem now includes the complete release lifecycle: deterministic source/chapter/chunk binding, production/counter-review/adjudication inputs, local dependency locks, immutable content-addressed release objects, publication, impact analysis, holds, selection, activation and transaction recovery.

All `literary/tools/alignment*.py` modules come from the working implementation. The public adaptation replaces corpus-specific inventory constants with the already-hashed manifest's explicit counts. It does not remove source hashes, sequential chapter identities, chunk hashes, review-role separation or release immutability.

Run `python alignment_demo.py` and `python -m unittest discover -s tests`. The demonstration builds a new three-chapter original corpus, freezes a structural checkpoint and saves/reloads an immutable **unreviewed** release. It never fabricates completed literary review. The synthetic tests verify source-drift refusal and object-tampering refusal as well as the round trip.

The existing compiler remains independent. For the separately deployable registry, bounded workers and React review interface, see [longtext-evidence-registry](https://github.com/PengdaoJiang/longtext-evidence-registry). Keeping these applications separate avoids pretending that their different corpus identities and review contracts have already been merged.
