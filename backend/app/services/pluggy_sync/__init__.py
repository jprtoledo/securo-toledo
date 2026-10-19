"""Pluggy-specific sync semantics (RFC toledo-features, Phase 2).

- ``sweeps``: what is fetched per account (date window ∪ ingestion window
  ∪ refetch of old pending rows);
- ``provider_update``: how an existing row follows the bank without
  touching what the user decided;
- ``removals``: soft removal by absence, with a safety guard;
- ``account``: the per-account orchestration used by connection_service.

Other providers keep the generic path in connection_service (INV-4).
"""
