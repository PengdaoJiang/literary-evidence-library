# Engineering cases from long-form curation

## Co-occurrence is not a relationship

The original project repeatedly rejected overbroad predicates: acting against the same opponent does not necessarily make characters allies; opening a place does not establish sovereignty; hiding somewhere does not make it a base. The correction belongs in reviewed literary records, not a higher automatic confidence score.

The public fixture uses a new scene about two people sharing shelter. A proposed alliance is rejected and remains outside the fact graph. The code enforces that separation; the scenario is a synthetic regression case for the design, not original novel evidence.

## A local event is not a continuing state

The schema distinguishes `relation_occurrences` from `relation_episodes`. An event can be retained as a direct fact without entering a long-lived relationship graph. Episodes point to supporting and challenging occurrences, keeping their time and identity context.

## Fixing identity should preserve history

An ambiguous mention can receive an explicit `endpoint_resolution` with old and new bindings. Rewriting the historical scan would erase what changed. The compiler retains the old binding and validates a new reviewed bridge with an exact source anchor.

## A rebuildable view is not the source of truth

The database is compiled from the input ledgers and text. A source-file change makes the stored inventory stale. `test_input_drift_is_reported` demonstrates that a readable database can still be out of date; `validate_database` reports the disagreement.
