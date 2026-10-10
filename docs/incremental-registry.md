# Incremental evidence registry

`experiments/book_registry/` preserves the transactional registry developed in a separate long-text research project. It is additive to the existing evidence compiler, not a replacement or a claim that the two applications are already integrated.

The retained implementation covers stable entities, mentions and evidence-bearing facts; modality and attribution; chapter task leases; exact quotation validation; idempotent submission; and review decisions. Transactions are short: a worker does not hold a database lock while reading or reasoning. Rejected naming facts are excluded from the derived name index without deleting their history.

```sh
python -B -m unittest discover -s experiments/book_registry -p test_registry.py -v
```

These are original protocol/concurrency tests with synthetic Chinese sentences. The test directory is generated under `outputs/` within this checkout. No novel corpus, model, external API, user feedback or live database is included.

SQLite uses explicit DELETE-journal transactions in this retained version. The registry checks structural integrity and evidence anchors; it cannot certify that a model understood the passage correctly or that extraction recall is complete. The research application has separately reviewed, pending and rejected records; the public tests are not those real decisions.

Code and synthetic fixtures use the repository MIT license. Python and SQLite retain their own licenses.
