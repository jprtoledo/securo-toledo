"""Pure step functions of the per-account recompute (RFC toledo-features §5.1).

Nothing here touches the database: each step takes plain row snapshots and
returns decisions, so every algorithm is unit-testable with plain objects.
``app.services.recompute_service`` loads the rows, runs the steps in order
and writes the diffs.
"""
