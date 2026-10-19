# RFC — Porting the toledo-finance features into Securo

| | |
|---|---|
| Status | Draft, ready for implementation |
| Target | `securo-toledo` (fork of `securo-finance/securo`, may diverge from upstream) |
| Source of the features | `toledo-finance` (TypeScript/Fastify/Drizzle, single user, Pluggy only) |
| Implementer | Claude Code (high effort). This document gives behavior + acceptance criteria, not code |
| Language of the product | Every user-facing string goes through i18n (`frontend/src/locales/*.json`) |

---

## 0. How to use this document

1. Implement **phase by phase** (§3–§8). Every phase is shippable alone and leaves the app working. Do not start a phase before the previous one's acceptance criteria pass.
2. Every phase has: *Today in Securo* → *Target behavior* → *Data model* → *Integration points* → *API* → *UI* → *Acceptance criteria (Gherkin)* → *Tests*.
3. **The reference implementation exists.** When an algorithm is described here, the original TypeScript lives in `/home/toledo/apps/toledo-finance` (paths in Appendix E). Read it before implementing — this RFC describes the *improved* version, so when the two disagree, **this RFC wins**.
4. Acceptance criteria IDs (`AC-2.7`) are stable: reference them in commit messages and test names.
5. When this RFC leaves a decision open (marked **Implementer's choice**), pick the option most consistent with surrounding Securo code and write it down in the PR.

### 0.1 Glossary

| Term | Meaning |
|---|---|
| Item | A Pluggy connection (one login at one institution). In Securo: `bank_connections` row with `provider = "pluggy"`, `external_id = item id` |
| Credential | A Pluggy *application* (client id + secret) from dashboard.pluggy.ai. An Item is only readable by the application that created it |
| Provider-owned field | A transaction field whose truth is the bank (amount, date, status, bill link, installment metadata, …) |
| User-owned field | A field the user (or a rule acting for the user) decides (category, renamed description, notes, recurrence decisions, …) |
| Recompute | The idempotent, per-account pass that derives everything that is *computed* from provider data + user decisions (§5) |
| Bill / statement | Credit card bill (fatura). `credit_card_bills` = bills Pluggy returned; *statement* = the per-month view (§6) that also covers bills Pluggy did not return yet |
| Reference month | The bill (credit card) or calendar month (bank account) a transaction belongs to. In Securo this is carried by `effective_date` |
| Plan | An installment purchase (compra parcelada) grouping its parcels |
| Period / slot | One expected occurrence of a recurrence |

### 0.2 Out of scope (explicitly decided)

- Tags, named budgets, consolidated balance projection, connection-health alert banner, a user-editable Pluggy-category mapping table — **Securo already covers these needs; do not port them.**
- Connection lifecycle (connect widget, reconnect, refresh-at-bank, error/expired states, sync scheduling) **stays as Securo does it today**, except where Phase 1 needs the credential (connect token, API key) and Phase 2 needs more data.
- Pluggy webhooks (Securo is self-hosted and usually not reachable from the internet). §4 compensates with fetch strategies. A webhook receiver can be a later RFC.
- Migrating toledo-finance data. The only migration tool is the recurrence import script (§8.9 / Appendix D).

---

## 1. Global invariants (apply to every phase)

- **INV-1 Workspace scoping.** Everything new that belongs to user data carries `workspace_id` and every query filters by it, exactly like existing models. The only instance-global entity is the Pluggy credential (§3).
- **INV-2 Provider vs user ownership.** Sync may overwrite provider-owned fields at any time. Sync **never** overwrites user-owned fields. Recompute **never** overwrites user decisions; it only (re)derives computed fields.
- **INV-3 Idempotency.** Running sync or recompute twice in a row with no new input changes nothing (zero UPDATEs on the second run — assert it in tests).
- **INV-4 Non-Pluggy providers are unaffected** unless a section says otherwise (SimpleFIN, Enable Banking, OFX/CSV import, manual entries keep their current behavior and tests).
- **INV-5 Money** is `Decimal`/`Numeric(15,2)`; never float in persistence or comparisons that decide links. Tolerances may be computed in Decimal.
- **INV-6 Totals.** Any row Securo must not count (provider-removed, duplicate) is excluded through the **existing** `is_ignored` mechanism (§4.6), so dashboards, reports, budgets and balances need no per-query change.
- **INV-7 Migrations** are Alembic revisions continuing the chain after `096_…`; run `backend/scripts/check_migration_chain.py`. Every migration has a working `downgrade`.
- **INV-8 i18n.** `frontend/src/locales/i18n.test.ts` requires every locale to contain every key of `en.json`: add new keys to **all** locale files (real translation in `en` and `pt-BR`; other locales may carry the English text).
- **INV-9 Tests.** Backend: pytest in `backend/tests/` (pure-function tests for every algorithm + service tests with the DB). Frontend: vitest next to the component. Existing tests must keep passing; when a behavior change is intended by this RFC, update the test and cite the AC.
- **INV-10 Explainability.** Every automatic decision that the user can contest (bill month, duplicate, plan link, recurrence link, rule effect) stores *why* (`*_source`, `*_issue`, `*_rule_id`) so the UI can explain it.

---

## 2. Architecture after this RFC

```
sync_connection (existing, Pluggy path changed)
 ├─ credential → API key (Phase 1)
 ├─ accounts / bills upsert (Phase 2: more fields)
 ├─ transactions fetch = union of sweeps (Phase 2)
 │    ├─ dateFrom window        (status/amount changes on recent rows)
 │    ├─ createdAtFrom watermark (late-ingested, back-dated rows, new parcels)
 │    └─ ids refetch of known PENDING rows (PENDING→POSTED outside the window)
 ├─ per transaction: match by external_id → providerId → insert   (Phase 2)
 │    └─ existing row: update provider-owned fields (INV-2)
 ├─ removal detection by absence, soft (Phase 2)
 └─ recompute_account(account) for every touched account   (Phase 3+)
        1. effective date / reference month + calibration   (Phase 4)
        2. duplicates (non-destructive)                     (Phase 2, runs here)
        3. card line kind (rules SET_CARD_KIND + defaults)  (Phase 3/4)
        4. installment plans + validation                   (Phase 5)
        5. recurrence links                                 (Phase 6)
        6. rule-driven classification (sticky semantics)    (Phase 3)
```

`recompute_account` is also triggered by user actions that change an input (rule created/edited, manual override, plan/recurrence decision, account cycle edit) — synchronously for one account, via a Celery task when a change affects many accounts (rule edits).

---

## 3. Phase 1 — Multiple Pluggy credentials

### 3.1 Today in Securo
One credential from `.env` (`PLUGGY_CLIENT_ID`, `PLUGGY_CLIENT_SECRET`), API key cached at **class level** in `PluggyProvider` (`backend/app/providers/pluggy.py`). Every connection uses it.

### 3.2 Target behavior
- The instance holds **N named Pluggy credentials**, managed **only by superusers** (`is_superuser`, `current_superuser` in `core/auth.py`) in the Admin page.
- Client id and secret are stored **encrypted with AES-256-GCM**, using a dedicated key `CREDENTIALS_ENCRYPTION_KEY` (32 bytes, base64 or 64 hex chars). The AAD of each ciphertext is `"<credential_id>:client_id"` / `"<credential_id>:client_secret"`, so a ciphertext copied to another row or field does not decrypt. Storage format: `v1.<iv>.<tag>.<ciphertext>` (base64url parts, 12-byte IV). Use `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
- A non-secret **hint** of the client id (`abcd…wxyz`; `…` when ≤ 8 chars) is stored in clear to recognize it in the UI. The API **never** returns the client id or the secret.
- **No validation call on save** (decided). Errors surface on first use, as today.
- Every Pluggy connection is **bound to one credential** (`bank_connections.pluggy_credential_id`). All calls for that connection (accounts, transactions, bills, investments, refresh, connect token in update mode) use its credential.
- Connecting a new Pluggy item: if the instance has exactly one credential it is used silently; with several, the connect dialog asks which one (non-admins see names only).
- API keys are cached **per credential** (dict keyed by credential id, invalidated when the credential row's `updated_at` changes). Expiry handling as today (2 h, refresh at < 5 min).
- **Bootstrap:** on startup, if no credential exists and `PLUGGY_CLIENT_ID/SECRET` are set, create one named `Default` from them and bind every Pluggy connection without credential to it. If credentials exist and the env vars are set too, log a warning that the env vars are ignored. Idempotent.
- A credential cannot be deleted while a connection references it (HTTP 409 with the count).
- Editing a credential: name and/or keys. Blank secret on edit = keep the current one.
- `CREDENTIALS_ENCRYPTION_KEY` missing/invalid: the app starts, Pluggy is reported "not configured" in the admin page with the reason, and creating a credential returns 400 with the generation hint (`openssl rand -base64 32`). Losing the key = re-enter the credentials (connections and data stay).

### 3.3 Data model
- New table `pluggy_credentials` (instance-global, no workspace): `id uuid pk`, `name text unique (case-insensitive)`, `client_id_enc text`, `client_secret_enc text`, `client_id_hint text`, `last_error text null` (last auth failure message seen at use time), `created_at`, `updated_at`.
- `bank_connections.pluggy_credential_id uuid null fk → pluggy_credentials.id ON DELETE RESTRICT`.

### 3.4 Integration points
- `core/config.py`: `credentials_encryption_key: SecretStr = ""`; keep `pluggy_client_id/secret` as legacy bootstrap only.
- New `core/secrets_crypto.py` (or `services/credential_crypto.py`): `encrypt(plain, context)`, `decrypt(payload, context)`, `hint(value)`. Do **not** reuse `agents/services/crypto.py` (Fernet, derived from `SECRET_KEY`) — different requirement.
- `providers/pluggy.py`: `PluggyProvider` receives the credential (or a resolver) instead of reading settings; `_ensure_api_key` caches per credential. `get_provider("pluggy")` call sites in `connection_service.py` / `tasks/sync_tasks.py` resolve the connection's credential.
- "Is Pluggy configured" checks (where the connector list is offered) become "at least one credential exists".
- `/api/admin` router: CRUD endpoints. `.env.example` + `install.sh` + Helm chart (`charts/securo`) document the new key.

### 3.5 API
- `GET /api/admin/pluggy-credentials` → `[{id, name, client_id_hint, connection_count, last_error, created_at, updated_at}]` (superuser).
- `POST /api/admin/pluggy-credentials` `{name, client_id, client_secret}` → 201 (superuser).
- `PATCH /api/admin/pluggy-credentials/{id}` `{name?, client_id?, client_secret?}` (superuser).
- `DELETE /api/admin/pluggy-credentials/{id}` → 204 or 409 (superuser).
- `GET /api/connections/pluggy-credentials` → `[{id, name}]` (any authenticated user; for the connect dialog).
- Connect-token creation accepts `pluggy_credential_id` (required when > 1 credential exists and no item id is given).

### 3.6 UI
- Admin page: "Pluggy applications" section — list (name, hint, number of connections, last error), add/edit dialog (secret field write-only with "leave blank to keep"), delete with blocking message.
- Connect dialog (`bank-connect-dialog.tsx` / `connector-select-dialog.tsx`): credential picker shown only when > 1 credential.
- Connection settings dialog: shows which application the connection uses.

### 3.7 Acceptance criteria

```gherkin
Feature: Multiple Pluggy credentials

  Background:
    Given CREDENTIALS_ENCRYPTION_KEY is a valid 32-byte key

  Scenario: AC-1.1 Superuser creates a credential, secrets are encrypted
    Given I am a superuser
    When I create a Pluggy credential named "Personal" with client id "abcd1234efgh5678" and a secret
    Then the response contains the hint "abcd…5678" and neither the client id nor the secret
    And the database row stores "v1."-prefixed ciphertexts for both values
    And decrypting the client id ciphertext with the AAD of another credential id fails

  Scenario: AC-1.2 Non-superusers cannot manage credentials
    Given I am a regular workspace member
    When I call any /api/admin/pluggy-credentials endpoint
    Then I receive 403
    But GET /api/connections/pluggy-credentials returns only id and name

  Scenario: AC-1.3 No validation call on save
    When a superuser saves a credential with wrong keys
    Then the credential is saved without calling Pluggy
    And the first sync using it fails with the Pluggy error message stored in last_error

  Scenario: AC-1.4 Connection uses its own credential
    Given credentials "A" and "B"
    And connection X is bound to "A" and connection Y to "B"
    When both connections sync
    Then every Pluggy HTTP call for X authenticates with A's keys and for Y with B's keys
    And each credential obtains and caches its own API key

  Scenario: AC-1.5 Choosing the credential when connecting
    Given two credentials exist
    When a user opens the Pluggy connect dialog
    Then they must pick an application before the widget opens
    And the resulting connection is bound to the chosen credential
    Given only one credential exists
    Then no picker is shown and that credential is used

  Scenario: AC-1.6 Reconnect (update mode) uses the bound credential
    Given connection X bound to credential "A"
    When the user reconnects X
    Then the connect token is created with credential "A" and item id of X

  Scenario: AC-1.7 Bootstrap from .env
    Given no credential exists and PLUGGY_CLIENT_ID/SECRET are set
    And two Pluggy connections have no credential
    When the backend starts
    Then a credential "Default" exists with those keys encrypted
    And both connections are bound to it
    When the backend starts again
    Then nothing changes and a warning says the env vars are ignored

  Scenario: AC-1.8 Deleting a credential in use is blocked
    Given credential "A" is used by 2 connections
    When a superuser deletes "A"
    Then the response is 409 mentioning 2 connections
    And "A" still exists

  Scenario: AC-1.9 Editing keeps the secret when left blank
    When a superuser renames "A" and leaves the secret blank
    Then the stored secret ciphertext decrypts to the previous secret
    And the cached API key for "A" is discarded

  Scenario: AC-1.10 Missing encryption key
    Given CREDENTIALS_ENCRYPTION_KEY is not set
    Then the app starts
    And creating a credential returns 400 explaining how to generate the key
    And the admin page shows Pluggy as not configured with that reason
```

### 3.8 Tests
Pure: encrypt/decrypt round trip, AAD mismatch, tampered tag, hint. Service: bootstrap idempotency, delete guard, per-credential API key cache (mock `httpx`), connect-token credential resolution.

---

## 4. Phase 2 — Pluggy data capture and sync semantics

### 4.1 Today in Securo
- `get_transactions` uses `/v2/transactions` with only `createdAtFrom = last_sync_at − 14 d`.
- For an existing row (same `external_id`) sync only merges `raw_data`, fills `external_id`/`payee` if empty, flips `pending→posted` and fixes the bill link. **Amount, description, date and installment metadata changes from the bank are never applied.**
- Pending↔posted twins and installment twins are **collapsed at ingestion** (`_find_synced_duplicate`), phantom duplicates are **hard-deleted** (`_cleanup_phantom_duplicates`).
- Removed transactions are never detected.
- Most Pluggy fields stay inside `raw_data` only.

### 4.2 Pluggy facts this phase relies on (from docs.pluggy.ai)
- Pluggy keeps the transaction `id` across syncs when its key matches (institution id, or description + amount + date + position); a change in description/amount/date makes it **delete + recreate with a new id**. `providerId` (Open Finance only) is the institution's stable id and survives that; `providerCode` is an institution code (format varies).
- Credit card: open-bill purchases are `PENDING` **without** `billId`; when the bill becomes due they become `POSTED` **with** `creditCardMetadata.billId`. Only the last 7 days are re-collected daily, so an old PENDING→POSTED change can fall outside a date window.
- Installments: Open Finance has no group id and no `totalAmount`; `purchaseDate`, `installmentNumber`, `totalInstallments` are available. Some banks post all parcels at once (future ones `PENDING`, dated by `billPostDate`), most post month by month (new rows with old dates → need `createdAtFrom`).
- Since Oct 2026 Pluggy synthesizes a `"Pagamento recebido"` transaction with `operationType = PAGAMENTO_FATURA` when the institution does not report the bill payment itself.
- Reading from Pluggy (`GET`) does **not** consume Open Finance quotas; only item updates do.
- `/v2/transactions` accepts `ids` (≤ 500 per request), `dateFrom`, `createdAtFrom`; page size 500 max.

### 4.3 Target behavior — fields captured
Promote to first-class columns (keep the whole payload in `raw_data` too). Exact names are **Implementer's choice** if they clash; semantics are not.

**Transaction** (`transactions`):
| Column | From Pluggy | Use |
|---|---|---|
| `provider_id` | `providerId` | stable reconciliation key (§4.5) |
| `provider_code` | `providerCode` | display/debug, secondary match hint |
| `provider_created_at`, `provider_updated_at` | `createdAt`, `updatedAt` | watermark (§4.4), debugging |
| `provider_description_raw` | `descriptionRaw` | rule field |
| `operation_type` | `operationType` | card line kind, transfer-like detection, rule field |
| `provider_category`, `provider_category_id` | `category`, `categoryId` | rule field; category mapping by id first, then the existing text map |
| `merchant_name`, `merchant_cnpj`, `merchant_mcc` | `merchant.name/businessName`, `merchant.cnpj`, `creditCardMetadata.payeeMCC` | rule fields, payee extraction |
| `card_number` | `creditCardMetadata.cardNumber` | additional/virtual card, rule field |
| `payment_type` | `creditCardMetadata.paymentType` (SINGLE/INSTALLMENT) | installment detection (§7) |
| `transaction_at` | `creditCardMetadata.transactionDateTime` / full `purchaseDate` timestamp | duplicate matching to the second (§4.7) |
| `provider_bill_external_id` | `creditCardMetadata.billId` | the bill link **as Pluggy sent it**, even when the bill row is not stored yet |
| `bill_forecast_month` | `creditCardMetadata.billForecastDate` (when present) | open-bill month (§6) |
| `bill_post_date` | `creditCardMetadata.billPostDate` (when present) | open-bill month for future parcels (§6) |
| `amount_in_account_currency` | `amountInAccountCurrency` | already used for `amount_primary`; store it raw too |
| `balance_after` | `balance` (direct connectors only) | display |
| `payment_data` (JSON) | `paymentData` | counterpart name/document; payee extraction already uses it |

**Account** (`accounts`): `available_credit_limit`, `balance_close_date`, `balance_due_date` (Pluggy `creditData`), `overdraft_limit`, `overdraft_used` (`bankData`), `card_holder_type`, `provider_tx_created_watermark` (§4.4). `statement_close_day`/`payment_due_day` keep existing semantics but are **derived** (§6.3) instead of typed.

**Bill** (`credit_card_bills`): `closing_date` (when Pluggy sends one), `allows_installments`, `payments` (JSON list as sent), `finance_charges_total`.

**Connection** (`bank_connections`, Pluggy only — informational, lifecycle unchanged): `provider_status`, `provider_execution_status`, `provider_last_updated_at`, `consent_expires_at`, stored on every sync. Showing them is optional (connection settings dialog).

Bills referenced by transactions (`provider_bill_external_id`) but missing from `/bills` are fetched with `GET /bills/{id}` at the end of the account sync (best effort, logged).

### 4.4 Target behavior — what is fetched
For each Pluggy account:
1. **First sync** of the account (no watermark): full history.
2. **Incremental sync** = union (dedup by id) of:
   - **Date window:** `dateFrom = today − PLUGGY_SYNC_LOOKBACK_DAYS` (setting, default 60).
   - **Ingestion window:** `createdAtFrom = provider_tx_created_watermark − 24 h`. The watermark is the max `provider_created_at` seen for the account, stored after a successful account sync (per account, not per connection).
   - **Pending refetch:** every stored row of the account with `status = pending`, older than the date window, not provider-removed, refetched by `ids` in batches of 500. An id that is not returned counts as *absent*.
3. All three use the connection's credential; failures in step 2b/2c degrade to a warning (the sync still succeeds with what it has).

### 4.5 Target behavior — matching an incoming transaction (Pluggy)
In order, first hit wins:
1. Same account + same `external_id` → **same row**.
2. Same account + same non-null `provider_id` (another `external_id`) → **same row, re-keyed**: replace `external_id`, then update as in §4.6. (Covers delete/recreate around a bill closing and reconnecting an account as a new item, as long as the account row is the same.)
3. Existing Securo passes that target **non-sync rows** stay: fuzzy match against manual entries, local-import history (Path 5 of `_find_synced_duplicate`), recurring placeholder upgrade.
4. Otherwise **insert**. Pending↔posted and installment twins are **not** collapsed at ingestion for Pluggy anymore — §4.7 handles them non-destructively. (`_find_synced_duplicate` Paths 1–3 and `_cleanup_phantom_duplicates` stay for other providers — INV-4.)

### 4.6 Target behavior — updating an existing row
- **Provider-owned fields are always updated** from the latest payload: `date`, `amount`, `currency`, `type`, `status`, `amount_in_account_currency`/`amount_primary`/`fx_rate_used` (recomputed), installment metadata, every §4.3 column, `raw_data` (replaced, not merged), bill link.
- **Description:** updated only while the user has not renamed it (`_has_manual_description` is false). `original_description` always follows the bank text. Rule-managed descriptions are re-derived by rules.
- **Never touched by sync:** category and its provenance, payee chosen by the user, notes, splits, attachments, `effective_bill_date` (manual bill override), recurrence/plan **decisions** (§7, §8 — accepted/rejected/manual links), `exclude_from_pnl`.
- **Sync lock:** new `transactions.sync_locked bool default false`, togglable by the user ("Don't update from the bank"). A locked row only gets `raw_data` and the `provider_*` columns refreshed; every visible field stays. Unlocking applies the latest stored payload immediately.
- Rows the user ignored (`is_ignored` set by the user) keep today's behavior: frozen (skipped).
- After updating `date`/`amount` the effective date is re-resolved (Phase 4 does it in recompute; until then call `apply_effective_date`), and the opening-balance reconciliation already in the sync loop runs as today.

### 4.7 Target behavior — removals and duplicates (non-destructive)
Both reuse `is_ignored` so every total excludes them (INV-6), with a reason so they can be reversed automatically:
- New columns: `ignored_reason text null` (`'user' | 'provider_removed' | 'duplicate'`; `null` on legacy rows = user), `provider_removed_at timestamptz null`, `duplicate_of_id uuid null fk → transactions.id ON DELETE SET NULL`.
- User ignore/unignore in the UI sets/clears `ignored_reason = 'user'`. Automatic reasons never override a user ignore, and the user unignoring an automatic one turns it into a protected "keep" (`ignored_reason = null`, plus `duplicate_override = true` for duplicates so recompute stops re-flagging it).
- **Removal by absence.** After an account sync, rows of that account with `source = sync`, not removed, inside the date window that was swept completely (or the whole history on a full sync), and not returned by any sweep, are marked `provider_removed_at = now`, `is_ignored = true`, `ignored_reason = 'provider_removed'`. **Safety guard:** if the account returned 0 transactions, or more than 5 rows and more than 30 % of the window would be removed, nothing is marked and a warning is logged. A removed row that comes back (same external_id or provider_id) is restored: both columns cleared, `is_ignored = false`.
- **Duplicates (Pluggy, credit card).** Computed in recompute (§5) from scratch every time:
  - A `PENDING` row without bill link duplicates a `POSTED` row with bill link of the same account when: same signed amount and same installment number (0 for none), at most 5 days apart — or same `installment_purchase_date` + same `n/N` with amount within the installment cent tolerance (§7.3). One-to-one assignment, closest pair first, ties by id.
  - Extra pending copies (bank sent it 2+ times) with the same `transaction_at` to the second + same `n/N` + amount within tolerance as a posted row are duplicates too (N:1).
  - Posted rows identified as **payment echoes** (§6.5) cannot absorb a pending row.
  - Flagged rows get `duplicate_of_id`, `is_ignored = true`, `ignored_reason = 'duplicate'`; rows that stop qualifying are unflagged. User overrides win.

### 4.8 Integration points
`providers/base.py` (`TransactionData`, `AccountData`, `BillData` gain the fields), `providers/pluggy.py` (`get_transactions` gains `date_from`, `created_at_from`, `ids` modes; `get_bill(id)`), `services/connection_service.py` (`sync_connection` Pluggy branch, `_merge_sync_metadata` split into "provider update" and "metadata merge"), new `services/pluggy_sync/` helpers if `connection_service.py` grows further (it is 2.5k lines — prefer new modules), `services/transaction_service.py` (ignore/unignore sets `ignored_reason`, sync lock endpoint), `api/transactions.py`.

### 4.9 UI
- Transaction detail: "Removed by the bank on …" badge (restorable = unignore), "Duplicate of …" link with "Not a duplicate" action, "Don't update from the bank" toggle, read-only provider info (operation type, card final, merchant, MCC, provider category, bill id).
- Transactions list filters: "Removed by the bank", "Duplicates".

### 4.10 Acceptance criteria

```gherkin
Feature: Pluggy sync keeps transactions current

  Scenario: AC-2.1 Provider changes are applied to existing rows
    Given a synced card purchase "UBER *TRIP" of R$ 25,00 dated 2026-10-01 with status pending
    When Pluggy returns the same id with amount 27,40, date 2026-10-02 and status posted
    Then the row has amount 27,40, date 2026-10-02 and status posted
    And raw_data equals the new payload

  Scenario: AC-2.2 User-owned fields survive sync
    Given a synced row whose category, notes and description were edited by the user
    When Pluggy returns the row with a different description and amount
    Then amount is updated
    And category, notes and the user's description are unchanged
    And original_description holds the new bank text

  Scenario: AC-2.3 Sync lock
    Given the user enabled "Don't update from the bank" on a row
    When Pluggy returns the row with a different amount
    Then amount, date, description and status are unchanged
    And raw_data and provider_* columns hold the new payload
    When the user disables the lock
    Then the row shows the latest bank values

  Scenario: AC-2.4 Re-keyed transaction matched by providerId
    Given a stored row with external_id "old" and provider_id "OF-123"
    When Pluggy returns external_id "new" with provider_id "OF-123" and no row "old"
    Then the stored row now has external_id "new"
    And no new row is inserted and the old row is not marked removed

  Scenario: AC-2.5 Back-dated rows are fetched
    Given the account watermark is 2026-10-10T00:00Z
    When Pluggy ingested on 2026-10-15 a parcel dated 2026-06-03
    Then the next sync stores that parcel
    And the watermark advances to the max provider_created_at

  Scenario: AC-2.6 Old pending rows are refetched by id
    Given a row pending since 2026-07-01, older than the 60-day window
    When Pluggy now reports it posted with a bill id
    Then the next sync updates it to posted with the bill link

  Scenario: AC-2.7 Removal by absence is soft and reversible
    Given 40 rows of a card inside the swept window
    When a sync returns 39 of them
    Then the missing row has provider_removed_at set, is_ignored true, reason provider_removed
    And it no longer counts in balances or reports
    When a later sync returns it again
    Then it is restored and counts again

  Scenario: AC-2.8 Removal safety guard
    Given 40 rows inside the swept window
    When a sync returns only 20 of them
    Then no row is marked removed and a warning is logged

  Scenario: AC-2.9 Pending copy flagged as duplicate, not deleted
    Given a pending row "PNEUSTORE 3/10" R$ 120,00 without bill on 2026-09-28
    And a posted row "PNEUSTORE 3/10" R$ 120,00 with bill on 2026-10-01
    When the account is recomputed
    Then the pending row has duplicate_of_id = the posted row and is ignored with reason duplicate
    And the posted row is untouched

  Scenario: AC-2.10 Installment cents drift still matches
    Given a pending parcel 2/6 R$ 50,01 and a posted parcel 2/6 R$ 49,99 with the same purchase date
    When the account is recomputed
    Then the pending parcel is a duplicate of the posted parcel

  Scenario: AC-2.11 User says "not a duplicate"
    Given a row flagged as duplicate
    When the user marks it "Not a duplicate"
    Then it counts again and later recomputes never flag it again

  Scenario: AC-2.12 Missing bill fetched by id
    Given a transaction references bill "B9" absent from /bills
    When the account syncs
    Then GET /bills/B9 is called and the bill is stored and linked

  Scenario: AC-2.13 Other providers unchanged
    Given a SimpleFIN connection
    When it syncs
    Then the existing dedup and phantom cleanup paths run exactly as before
```

### 4.11 Tests
Pure: sweep union/dedup, removal guard, duplicate matcher (1:1, nearest first, cents tolerance, N:1 by timestamp, echo exclusion). Service: provider update vs user-owned fields matrix, sync lock, providerId re-key, restore after removal, SimpleFIN regression.

---

## 5. Phase 3 — Recompute engine and Rules v2

### 5.1 Recompute engine
- New `services/recompute_service.py` with `recompute_account(session, account_id) -> RecomputeStats`.
- Loads the account's rows once, runs the steps of §2 in order, computes **diffs** and writes only changed rows (bulk `UPDATE … FROM (VALUES …)`, chunks of 1000), in one DB transaction.
- Pure step functions (no DB) in `services/recompute/` so each algorithm is unit-testable with plain objects.
- Triggers: end of sync for touched accounts; manual overrides (bill month, cycle, plan/recurrence decision, duplicate override, sync unlock); rule create/update/delete/enable (Celery task `recompute_workspace(workspace_id, scope)` over the accounts the rule's old **and** new scope reach); recurrence create/update/delete; an admin "Recompute" button per account (debug).
- Returns stats (changed rows, duplicates, plans, conflicts, links) — logged, and returned by the manual endpoints.

### 5.2 Rules — today in Securo
`rules` has `conditions_op`, `conditions` (leaves or one level of groups), `actions` (`set_category`, `set_payee`, `set_description`, `append_notes`, `ignore`), `priority`, `is_active`. Fields: description, payee, notes, amount, type, account_id, payee_id, date, status. Ops: contains, not_contains, starts_with, ends_with, equals, not_equals, regex, gt/gte/lt/lte. Applied once when a transaction is created; first category wins and never overwrites an existing category; retroactive application is an explicit action. Rule packs, export/import and preview exist.

### 5.3 Rules — target behavior
Extend, keep everything that exists working (old JSON is valid v2 JSON).

**Scope** — new columns `scope_account_kind` (`null` = all, `'bank'` = checking/savings, `'credit_card'`) and `scope_account_id` (null or an account of the workspace; setting it forces the kind to that account's). Fields, operators and actions offered/validated depend on the scope.

**Conditions**
- Arbitrary nesting: a node is a leaf `{field, op, value}`, a group `{op: "and"|"or", conditions: [...]}`, or `{not: node}`. The top level stays `conditions_op` + `conditions`. Max depth 5.
- New fields (credit-card-only marked †): `description_key` (description with letters only, uppercased, accents stripped — "APPLE.COM/BILL" and "Apple.com/bill" → `APPLECOMBILL`), `original_description`, `provider_description_raw`, `merchant_name`, `signed_amount` (− outflow / + inflow), `currency`, `operation_type` (enum), `provider_category`, `card_number`†, `installment_number`†, `total_installments`†, `payment_type`† (enum), `account_kind`.
- New ops: `in`, `not_in` (list), `not_regex`, `is_empty`, `is_not_empty`. Enum fields only allow equals/not_equals/in/not_in/is_empty/is_not_empty. Value-less ops must not carry a value; every other op requires one (keep today's "blank value never matches" protection).
- The same condition language is used by recurrences (§8) — one evaluator: extend `services/rule_engine.py` (`evaluate_conditions`) and its validator (`_validate_rule_definition`).

**Actions** (new ones in bold):
| Action | Scope | Effect |
|---|---|---|
| `set_category` | all | category (sticky semantics below) |
| **`clear_category`** | all | removes the category (sticky semantics) |
| `set_payee`, `set_description`, `append_notes`, `ignore` | all | unchanged |
| **`set_card_line_kind`** `{kind: purchase\|credit\|payment\|previous_balance}` | credit_card | classifies a card row for statements (§6.5); highest-priority match wins |
| **`strip_installment_marker`** `{pattern}` | credit_card | regex removed from the description **only** when grouping parcels and naming plans (§7); the stored description is not changed |

**Sticky classification semantics (improved toledo logic)** — new transaction columns `category_source` (`'provider' | 'manual' | 'rule' | null`) and `category_rule_id`:
1. On every recompute, the highest-priority active rule in scope whose condition matches and that has a category action decides — **unless** the category is `manual`.
2. A manual category choice is sticky: no rule overrides it automatically. To let rules take over again the user either clears the category (source back to null) or uses the existing "Apply rule to existing transactions" with the new option **"Also override manual choices"**.
3. When the deciding rule stops matching (edited, disabled, deleted, transaction changed), the category stays, `category_rule_id` becomes null, source stays `rule` — so a different rule can still replace it later.
4. With no rule and no manual choice, the provider mapping applies (`provider`, by `provider_category_id` first, then the existing `PLUGGY_CATEGORY_MAP` text map) and follows the mapping when it changes.
5. Hidden/deleted categories in actions are skipped, as today.
- `set_description` keeps `description_is_rule_managed` semantics. `ignore` by rule sets `ignored_reason = 'user'` only at creation time, as today (not re-evaluated, to avoid fighting the user).
- `set_card_line_kind` and `strip_installment_marker` are **derived**: evaluated in recompute every time, never persisted into user-owned fields. The resolved kind is stored in `transactions.card_line_kind` + `card_line_kind_source` (`'rule' | 'default'`) + `card_line_kind_rule_id` for queries and explanation.

**Built-in pack "Brazilian credit cards (Open Finance)"** — install by default for new workspaces and offer through the existing rule-pack mechanism for existing ones; rules are editable/disableable, flagged `is_builtin` with a stable `builtin_key`, and "Restore defaults" recreates the originals:
| Key | Scope | Condition | Action |
|---|---|---|---|
| `br.previous_balance` | credit_card | description regex `(TOTAL DA )?FATURA ANTERIOR\|SALDO ANTERIOR` | kind previous_balance |
| `br.payment_by_text` | credit_card | type = credit AND description regex `\b(PGTO\|PAGTO\|PAGAMENTO)\b` AND not_regex `AJUSTE\|ESTORNO` | kind payment |
| `br.marker_parc_eq` | credit_card | description regex `\bPARC\s*=\s*\d{1,3}` | strip `\bPARC\s*=\s*\d{1,3}\s*` |
| `br.marker_parc` | credit_card | regex `\bPARC(ELA)?\.?\s*\d{1,3}` | strip `\bPARC(?:ELA)?\.?\s*\d{1,3}(?:\s*(?:/\|DE)\s*\d{1,3})?\b` |
| `br.marker_slash` | credit_card | regex `\b\d{1,3}\s*/\s*\d{1,3}\b` | strip same |
| `br.marker_de` | credit_card | regex `\b\d{1,2}\s+DE\s+\d{1,2}\b` | strip same |

**Validation surfaced, not hidden** — `GET /api/rules` returns `errors: []` per rule (unknown field for the scope, operator/field mismatch, invalid regex, missing value, action not allowed for the scope, category no longer exists). Invalid rules are skipped by the engine and shown with a warning in the UI.

### 5.4 Integration points
`models/rule.py` (+ `scope_account_kind`, `scope_account_id`, `is_builtin`, `builtin_key`), `services/rule_engine.py`, `services/rule_service.py` (validation, packs, preview counts must reflect sticky semantics), `services/connection_service.py` & import path (creation-time application keeps working through the same engine), `models/transaction.py`, export/import format (bump version, still read v1), MCP tools under `backend/mcp_server/tools` that create rules (accept the new shapes), `frontend/src/pages/rules.tsx` (nested group editor, scope selector, new fields/ops/actions, per-rule errors).

### 5.5 Acceptance criteria

```gherkin
Feature: Rules v2

  Scenario: AC-3.1 Old rules keep working
    Given a rule saved before this change with conditions_op "and" and one group
    When transactions are synced
    Then the rule matches exactly the same transactions as before

  Scenario: AC-3.2 Scope limits rules and their fields
    Given a rule scoped to credit cards using field installment_number
    Then it never evaluates bank account transactions
    And saving a rule scoped to "all" with field installment_number is rejected

  Scenario: AC-3.3 Nested and negated conditions
    Given a rule "type is debit AND NOT (description_key starts with UBER OR merchant_name in [99, Cabify])"
    Then it matches "IFOOD *RESTAURANTE" debit and does not match "UBER *TRIP" debit

  Scenario: AC-3.4 description_key ignores punctuation, digits and accents
    Then a condition description_key starts_with "APPLECOMBILL" matches "APPLE.COM/BILL 123" and "Apple.com/bill"

  Scenario: AC-3.5 Manual category is sticky
    Given a rule sets "Transport" for description contains "UBER"
    And the user manually sets "Work" on one UBER transaction
    When the account is recomputed
    Then that transaction stays "Work" with category_source manual
    And other UBER transactions are "Transport" with category_source rule and category_rule_id set

  Scenario: AC-3.6 Override manual choices explicitly
    When the user applies the rule to existing transactions with "Also override manual choices"
    Then the "Work" transaction becomes "Transport" with source rule

  Scenario: AC-3.7 Rule that stops matching leaves the value
    Given a transaction categorized "Transport" by rule R
    When R is disabled
    Then the transaction is still "Transport", category_rule_id is null, source rule
    And a later matching rule with a category action may replace it

  Scenario: AC-3.8 Rule edits are retroactive
    When a rule's condition is edited
    Then a recompute runs for every account in the old or new scope
    And transactions that now match get the category, unless manual

  Scenario: AC-3.9 Provider mapping by id
    Given no rule matches and the user never chose a category
    When a transaction arrives with provider_category_id mapped to "Groceries"
    Then it is "Groceries" with category_source provider

  Scenario: AC-3.10 Card line kind by rule
    Given the built-in rule br.previous_balance
    When a card row "TOTAL DA FATURA ANTERIOR" arrives
    Then card_line_kind is previous_balance with source rule and the rule id

  Scenario: AC-3.11 Strip marker does not change the description
    Given the built-in rule br.marker_de
    Then the row "SHEIN COM 02 DE 06" keeps that description
    And its plan (Phase 5) is named "SHEIN COM"

  Scenario: AC-3.12 Restore defaults
    Given the user edited br.payment_by_text and deleted br.marker_slash
    When they restore defaults
    Then both rules exist again with their original definition

  Scenario: AC-3.13 Invalid rules are visible and skipped
    Given a rule whose category was deleted
    Then GET /api/rules returns an error for it
    And the engine skips it without failing the sync

  Scenario: AC-3.14 Recompute is idempotent
    When an account is recomputed twice with no change in between
    Then the second run writes zero rows
```

### 5.6 Tests
Pure: evaluator truth tables per op/field/type, nesting and `not`, validator matrix, sticky classification state machine (provider → rule → manual → rule stops matching → other rule), card kind priority. Service: retroactive recompute on rule edit (old+new scope), pack install/restore, export/import v1→v2.

---

## 6. Phase 4 — Credit card statements and reference month

### 6.1 Today in Securo
`apply_effective_date` (`services/credit_card_service.py`): manual `effective_bill_date` > bill due date (when `bill_id`) > cycle math from `statement_close_day`/`payment_due_day` (typed by the user or from Pluggy `creditData` days). Bills are listed (`GET /accounts/{id}/bills`) and finance charges become synthetic `bill_charge:*` transactions. Accrual mode (global admin setting) makes aggregations use `effective_date`.

### 6.2 Target behavior — resolution order (per credit card row, in recompute)
`effective_date` stays the single column every report reads; a new `effective_date_source` explains it:

1. `manual` — `effective_bill_date` set by the user (unchanged).
2. `bill` — the row carries `provider_bill_external_id` of a stored bill → that bill's `due_date`. **This is the normal case for every posted card row** (Pluggy links every posted row to its bill — no closing date needed).
3. Open-bill / future rows (no bill link yet), first method that yields a value, in the account's **calibrated order**:
   - `forecast` — `bill_forecast_month` (+ learned month offset) → due date of that month.
   - `post_date` — `bill_post_date` placed in the cycle (future parcels of "post all at once" banks).
   - `cycle` — purchase `date` placed in the cycle.
4. `tx_date` — nothing else available (and always for bank accounts: `effective_date = date`).

**Calibration** (per account, every recompute): using rows that have a real bill as ground truth, learn `forecast_offset_months` (mode of `bill month − forecast month`) and measure each fallback's hit rate; order fallbacks by hit rate when a method has ≥ 5 samples, else by the prior `forecast > post_date > cycle`; stable ties. Persist on the account (`resolution_stats` JSON: samples, hits per method, offset, order) and show it in a "How we place open-bill purchases" tooltip.

### 6.3 Target behavior — cycle without typed days
"Placing a date in the cycle" uses, in order:
1. **Real bill boundaries:** a date falls in the bill whose period contains it. A bill's period ends the day before its closing date (purchases on the closing day go to the next bill); its closing date is Pluggy's `closing_date` when present, otherwise derived from the **latest posted rows** of that bill (the day after the last purchase date of bill N and before the first of bill N+1 bounds it) — **Implementer's choice** of the exact derivation, documented in code. The first known bill only accepts dates at most 35 days before its end.
2. Projection by `closing_day`/`due_day`, which are now **derived**: mode of the last 6 bills (due day from `due_date`, closing day from closing dates), else Pluggy `balance_close_date`/`balance_due_date` of the account. The user may still override them (existing edit dialog); an override sets `cycle_days_manual = true` and the derived values stop overwriting it; clearing the override goes back to derived.
3. If due day > closing day the bill is due the same month it closes, else the next month.

### 6.4 Target behavior — statement view
New `GET /api/accounts/{id}/statements` returns one entry per month from the first bill/row to the last future parcel/forecast:

| Field | Meaning |
|---|---|
| `month` | due month |
| `state` | `closed` (bill from Pluggy), `closed_estimated` (cycle already closed, bill not returned yet), `open` (current cycle), `future` |
| `closing_date`, `due_date`, `dates_estimated` | from the bill or the cycle |
| `total`, `total_estimated` | bill total (closed) or computed (others; includes projections in open/future) |
| `posted_total` | without projections |
| `spent` | purchases − credits of the cycle |
| `prepaid` | payments made during the cycle beyond what settled the previous statement |
| `paid`, `paid_at`, `payments[]` | payment rows applied to this statement (see below) |
| `status` | `paid`, `due`, `overdue`, `nothing_due`, `not_closed`, `unknown` (statement before the first synced row) |
| `minimum_payment`, `finance_charges` | from the bill |
| `reconciliation` | closed only: `{computed, diff}` = bill total vs sum of its rows |
| `history` | `before` / `first` / null — coverage of the transaction history |
| `projected_installments`, `projected_recurring` (+ counts) | from Phases 5 and 6 (0 until those phases ship) |

Rules of the computation (reference: `toledo-finance/apps/api/src/domain/card-statement.ts`, improved):
- Row kinds from `card_line_kind` (§5.3): only `purchase`/`credit` count as spending; `payment` rows are payments; `previous_balance` rows are carried balance.
- **Ledger banks** (a statement containing a `previous_balance` row, e.g. CAIXA): the computed total is the sum of all its rows. Otherwise (e.g. Sicredi): `spent − prepaid`.
- **Payment allocation** uses payment rows only (Pluggy `bill.payments` is inconsistent across banks; keep it for display): a payment between closing N and closing N+1 settles statement N up to its total; the excess is prepayment of N+1.
- Securo's synthetic finance-charge rows (`bill_charge:*`) count as purchases of their bill (they are what makes the computed total reconcile).
- Pluggy's settlement formula (`total(N) + financeCharges(N+1) = payments(N+1)`) is used as a cross-check: when it holds, `status = paid` even if payment rows are missing.

### 6.5 Bank quirks handled as data, not code paths
- **Previous balance as a row** (CAIXA): built-in rule → kind `previous_balance`.
- **Payment by text** (CAIXA does not use `PAGAMENTO_FATURA`): built-in rule → kind `payment`.
- **Default kind** when no rule: `credit` + `operation_type = PAGAMENTO_FATURA` → `payment`; other `credit` → `credit`; `debit` → `purchase`.
- **Carried-over credits** (CAIXA re-sends in bill N the credits already in N−1, same date and amount, compensated by an inflated previous balance): matched 1:1 by `(month−1, date, amount)` ignoring description → treated as `previous_balance` for statement math (stored as kind with source `default`, reason `carried_over`).
- **Payment echoes** (Pluggy-synthesized "Pagamento recebido" with `PAGAMENTO_FATURA` on ledger banks that also post their own payment row): in a statement that has a `previous_balance` row, `PAGAMENTO_FATURA` payment rows are echoes → ignored with reason `duplicate` (they are receipts, not ledger lines) and never absorb pending rows (§4.7).

### 6.6 Integration points
`services/credit_card_service.py` (resolution moves to a pure resolver used by recompute; `apply_effective_date` stays as the creation-time call), `services/recompute/`, `services/connection_service.py` (stop typing cycle days from Pluggy into the manual columns; they become derived), `models/account.py` (`cycle_days_manual`, `forecast_offset_months`, `resolution_stats`), `models/transaction.py` (`effective_date_source`), `api/accounts.py`, `frontend/src/pages/account-detail.tsx` (statement timeline per month with state/status badges, reconciliation diff, projected parts, "why this month?" popover on rows).

### 6.7 Acceptance criteria

```gherkin
Feature: Credit card statements

  Scenario: AC-4.1 Posted rows use their bill
    Given a posted row linked to bill B due 2026-11-10
    Then its effective_date is 2026-11-10 with source bill

  Scenario: AC-4.2 Manual override wins
    When the user moves that row to the December bill
    Then effective_date is in December with source manual
    And later syncs and recomputes keep it there

  Scenario: AC-4.3 Open-bill purchase uses the calibrated fallback
    Given 20 posted rows show bill month = forecast month + 1
    And a pending row with bill_forecast_month 2026-10
    When the account is recomputed
    Then forecast_offset_months is 1
    And the pending row is placed in the November bill with source forecast

  Scenario: AC-4.4 Fallback order follows measured accuracy
    Given post_date hits 19/20 and forecast hits 12/20 on ground-truth rows
    Then the account's fallback order starts with post_date

  Scenario: AC-4.5 Cycle derived from bills, no typed days
    Given the last 6 bills are due on day 10 and close on day 3
    And the user never typed cycle days
    Then the account's derived closing_day is 3 and due_day is 10
    And a purchase on the 3rd goes to the next bill

  Scenario: AC-4.6 User cycle override sticks
    When the user sets closing 28 / due 8
    Then recompute uses 28/8 and never overwrites them
    When the user clears the override
    Then the derived days are used again

  Scenario: AC-4.7 Statement states
    Given today is 2026-10-17, the October bill was returned, the cycle closing 2026-10-25 is open
    Then October is closed, November is open and December (with future parcels) is future
    And a cycle that closed on 2026-10-03 whose bill Pluggy has not returned yet is closed_estimated

  Scenario: AC-4.8 Payment allocation and prepayment
    Given statement September total R$ 1.000 and a payment row of R$ 1.200 between its closing and October's closing
    Then September is paid on that date
    And October shows prepaid R$ 200 reducing its total

  Scenario: AC-4.9 Ledger bank total
    Given a CAIXA statement containing "TOTAL DA FATURA ANTERIOR" R$ 500, purchases R$ 300 and "PGTO.BOLETO" −R$ 500
    Then the computed total is R$ 300 and reconciliation diff against the bill is 0

  Scenario: AC-4.10 Payment echo
    Given that CAIXA statement also has a Pluggy "Pagamento recebido" PAGAMENTO_FATURA −R$ 500
    Then that row is ignored with reason duplicate and the total is still R$ 300

  Scenario: AC-4.11 Carried-over credit
    Given an "ESTORNO" credit of R$ 40 on 2026-09-12 in the September statement
    And the same credit (same date and amount, other text) in the October statement
    Then the October copy does not reduce October's spending

  Scenario: AC-4.12 Unknown status before history
    Given a bill due before the first synced transaction of the card
    Then its status is unknown and history is before

  Scenario: AC-4.13 Reports follow effective dates
    Given accrual mode
    Then the dashboard and budgets count each card row in its effective_date month exactly as the statement view does
```

### 6.8 Tests
Pure: resolver precedence, calibration (offset mode, ordering, < 5 samples prior), cycle placement on bill boundaries + projection + month rollover + day clamping (31 → 30/28), statement builder with fixtures for Sicredi-like and CAIXA-like cards (build fixtures from the anonymized shapes in Appendix C), settlement cross-check.

---

## 7. Phase 5 — Installment plans

### 7.1 Today in Securo
Synced parcels keep `installment_number/total_installments/installment_total_amount/installment_purchase_date` only as a dedup fingerprint; category is inherited from the previous parcel; manual series (`POST /transactions/installments`) materialize N rows with `installment_series_id` and support `apply_to=this|future|all` edits. No plan view, no projection of parcels the bank has not sent.

### 7.2 Target behavior
- New `installment_plans` table: `id`, `workspace_id`, `account_id`, `source` (`detected | manual | series`), `key` (heuristic key, detected only, unique per account), `description` (display name), `total_installments`, `installment_amount` (latest parcel), `first_month` (reference month of parcel 1), `purchase_date`, `category_id` (latest user choice among its parcels), timestamps.
- Transaction columns: `installment_plan_id` (derived), `installment_link` (`auto | conflict | manual`), `installment_issue` (text), `installment_plan_manual_id` (user decision; recompute respects it and never re-evaluates that row).
- Manual series (`installment_series_id`) are exposed as plans with `source = series` (one plan per series id, created/updated by the series endpoints) so the plan view is uniform; they are never regrouped by the heuristic.

**Detection (recompute, per card, live rows only — not removed, not duplicate):**
1. A row is a parcel when `payment_type != SINGLE`, `total_installments > 1`, `installment_number ≥ 1`.
2. Group key: `account + purchase_date (day) + total_installments`. Without purchase date: `account + normalized description + total + origin month`, where normalized description = description with the matching `strip_installment_marker` patterns removed, accents stripped, lowercased, non-alphanumerics collapsed; origin month = reference month − (n − 1).
3. If a group has two parcels with the same number (e.g. three 6x purchases at the same shop on the same day), split it by amount: process from the highest parcel number down (middle parcels have no cent adjustment), join a cluster when the amount is within tolerance and the number is free; each cluster gets the key suffix `|<amount>`.
4. Tolerance: parcels of the same purchase differ by at most `(N + 1)` cents (rounding of total/N lands on one parcel).
5. `first_month` = mode of `reference month − (n − 1)` over the group.
6. **Validation:** parcel k must be in `first_month + (k − 1)` (`MONTH` issue) and within tolerance of the median parcel amount (`AMOUNT` issue). Failing rows stay in the plan with `installment_link = conflict` and a human-readable `installment_issue` (i18n key + params).
7. Detected plans that lose all parcels are deleted, unless a parcel was manually linked to them → they become `manual` and keep only the manual parcels.
8. Plan ids are stable while the key is stable (upsert by `(account_id, key)`).

**Projection:** parcels not received yet are projected at `first_month + (n − 1)` for `n > max known` and month ≥ the open statement month. Parcels missing in **closed** months are reported as `missing` (bank did not send) or `before_history` (before the first synced row) — never projected. Projected parcels feed `projected_installments` of statements (§6.4) and the existing dashboard/forecast surfaces that show future card charges (**Implementer's choice** where — at least the account detail and the dashboard's upcoming card bill).

**User decisions** (each one triggers a recompute of the account):
- Link a parcel to another plan of the same card with the same `total_installments` and a free number → `manual`.
- Split a parcel into its own new plan (`source = manual`).
- Merge plan A into plan B (same card, same N, no overlapping numbers) → every parcel of A manual-linked to B.
- Confirm a conflict → becomes `manual` on the current plan.
- Undo → removes the decision, back to the heuristic.
- Category: changing the category of a parcel offers "apply to the whole purchase" (existing `apply_to` semantics) and the plan's `category_id` follows the latest choice; new parcels of the plan get it (replaces `_find_installment_category`).

### 7.3 API
- `GET /api/accounts/{id}/installment-plans` → `{open_month, alerts: {conflicts, missing}, plans: [{id, source, description, purchase_date, total_installments, installment_amount, total_amount, first_month, last_month, current_installment, remaining, active, conflicts, missing, installments: [{number, month, amount, kind: posted|projected|missing|before_history, transaction_id, pending, link, issue}]}]}`.
- `POST /api/transactions/{id}/installment-plan` `{plan_id: uuid | "new" | null}`.
- `POST /api/installment-plans/{id}/merge` `{into_plan_id}`.
- `POST /api/transactions/{id}/installment-plan/confirm`.

### 7.4 UI
Account detail (credit card): "Installments" tab — active plans first, each with a parcel timeline (posted / projected / missing / conflict), remaining amount, actions (link, split, merge, confirm). Transaction row: "3/10 · PNEUSTORE" chip opening the plan.

### 7.5 Acceptance criteria

```gherkin
Feature: Installment plans

  Scenario: AC-5.1 Parcels grouped by purchase date
    Given parcels 1/3, 2/3 of "PARC=103 PNEUSTORE" with purchase date 2026-08-14 in the August and September bills
    Then one plan exists named "PNEUSTORE" with 3 installments and first month August
    And parcel 3/3 is projected in October

  Scenario: AC-5.2 Description changes do not split the plan
    Given parcel 1/6 "SHEIN" and parcel 2/6 "SHEIN COM 02 DE 06" with the same purchase date
    Then both belong to the same plan

  Scenario: AC-5.3 Same-day purchases at the same shop are separated by amount
    Given three 6x purchases at SHEIN on 2026-07-02 with parcels R$ 33,10, R$ 49,90 and R$ 80,00
    Then three plans exist, one per amount

  Scenario: AC-5.4 First parcel with extra cents is accepted
    Given a 12x plan with parcel 1 R$ 7,03 and parcels 2..12 R$ 6,92
    Then no parcel is in conflict

  Scenario: AC-5.5 Out-of-sequence parcel becomes a conflict
    Given a plan whose first month is August
    When parcel 3/10 lands in the November bill
    Then it stays in the plan with link conflict and issue "expected in the October bill"

  Scenario: AC-5.6 Manual link survives recomputes
    When the user links a parcel to plan P
    Then the parcel shows link manual on P after any number of syncs and recomputes

  Scenario: AC-5.7 Merge rejected on overlapping numbers
    Given plans A and B both have parcel 2/6
    When the user merges A into B
    Then the request fails explaining both have parcel 2

  Scenario: AC-5.8 Missing vs projected
    Given today's open statement is November
    And a 10x plan with parcels 1, 2, 4 received (August, September, November)
    Then parcel 3 (October, closed) is missing
    And parcels 5..10 are projected from December to May
    And the November statement's projected_installments does not include parcel 4 (already posted)

  Scenario: AC-5.9 Single payment with counter is not a plan
    Given a row with payment_type SINGLE and "1/12" in installment fields
    Then it belongs to no plan

  Scenario: AC-5.10 Category follows the purchase
    Given the user set "Car" on parcel 2/3 of a plan
    When parcel 3/3 arrives
    Then parcel 3/3 is "Car"

  Scenario: AC-5.11 Manual series are shown as plans
    Given a manual series of 4 parcels created through POST /transactions/installments
    Then the installment plans endpoint lists it with source series
    And the heuristic never regroups its parcels
```

### 7.6 Tests
Pure: grouping (purchase date / fallback key / collision split), tolerance, validation issues, projection (missing vs projected vs before history), plan lifecycle (delete orphan, keep manual). Service: decisions + recompute, merge guard, category propagation, series exposure.

---

## 8. Phase 6 — Recurrences v2

### 8.1 Today in Securo
`RecurringTransaction`: exact `amount`, `type`, frequencies weekly/biweekly/monthly/quarterly/semiannual/yearly, `weekend_adjustment`, `day_of_month`, `start_date/end_date`, `next_occurrence` pointer, `auto_generate` placeholders. Matching (`recurring_match_service`) happens **once per incoming transaction**: same account/direction/currency, **exact amount**, −3/+5 days (policy), description token overlap ≥ 0.6; then `advance_past` moves the pointer. Projections come from `get_occurrences_in_range` (dashboard, transaction calendar).

### 8.2 Target behavior — model (extends `recurring_transactions`)
| Column | Meaning |
|---|---|
| `match_condition` JSON null | condition in the rules language (§5.3) that recognizes the charges; null = legacy matcher (§8.4) |
| `amount_mode` `fixed\|variable` (default fixed) | fixed: expectation follows the last confirmed amount; variable: mean of the last 3 confirmed periods |
| `amount_tolerance_pct` Numeric(5,2) (default 0) | allowed deviation from the expected amount; minimum absolute tolerance R$ 0,05 |
| `day_window` smallint null | ± days around the expected date; null = default per frequency (weekly 2, biweekly 3, monthly/quarterly/semiannual 5, yearly 10, weekly_billed_monthly 5) |
| `frequency` | existing values + **`weekly_billed_monthly`** |
| `billing_weekday` smallint null | weekly_billed_monthly: weekday of the service (0 = Monday) |
| `week_count_basis` `previous_month\|same_month` null | weekly_billed_monthly: which month's weekday count sets the charge |
| `origin` `manual\|suggestion\|import` | provenance |
| `import_ref` text null, unique per workspace | idempotency key of the import script (`toledo:<uuid>`) |
| `auto_generate` | kept; **default becomes false** — forecasts are computed (decided). Existing `true` rows keep generating placeholders until the user turns it off |

`amount` keeps meaning "reference amount" (for `weekly_billed_monthly`: **amount per week/unit**). `next_occurrence` is kept up to date by recompute (= first expected/late slot) so every existing reader keeps working.

Transaction columns (derived unless marked *decision*): `recurring_transaction_id` (exists), `recurring_period` date, `recurring_link` (`auto | conflict | manual`), `recurring_issue` text, `recurring_companion` bool, `recurring_manual_id` (*decision*), `recurring_manual_none` bool (*decision* "not recurring"), `recurring_manual_excluded` bool (*decision* "accept but keep out of the expected amount"), `recurring_rejected_ids uuid[]` (*decision* "never auto-link to these"). **Sync never touches the decision columns** (INV-2).

### 8.3 Target behavior — periods and expectations
- **Periods:** weekly = every 7 days from `start_date`; biweekly = every 14 days; monthly/quarterly/semiannual/yearly = month steps 1/3/6/12 from the start month, at `day_of_month` clamped to the month length; weekly_billed_monthly = monthly at `day_of_month` (the billing day). `end_date` closes the series. `weekend_adjustment` shifts the expected date as today.
- **Expected date** of a month-based period: the day of the last confirmed charge that fell in its own period month (subscriptions that moved day), else `day_of_month`. Weekly/biweekly keep the cadence from `start_date` (no drift).
- **Expected amount:** reference `amount` until there is history; fixed = last confirmed main charge; variable = mean of the last 3 confirmed periods; periods accepted with "keep out of the expected amount" are skipped; one value per period (the main charge, not the IOF companion).
- **weekly_billed_monthly:** units(period) = number of `billing_weekday` occurrences in the basis month (the period's month for `same_month`, the previous month for `previous_month`); expected = per-unit × units. History learns the **per-unit** value (confirmed amount ÷ its units), so a raise is followed. Example: cleaner every Tuesday at R$ 150/week, billed on the 5th for the previous month: the charge on 2026-10-05 expects 5 × 150 = R$ 750 (September 2026 has 5 Tuesdays), the one on 2026-11-05 expects 4 × 150 = R$ 600.
- **Foreign currency:** the expectation is in the recurrence currency; the BRL estimate uses the FX (and IOF) of the last confirmed charge, else the median FX of the account's last 90 days.

### 8.4 Target behavior — linking (recompute, per account, from scratch)
Candidates: live rows (not removed, not duplicate, not `recurring_manual_none`) whose direction and currency match, not in `recurring_rejected_ids`, and that satisfy the matcher. Card rows count only when `card_line_kind` is `purchase` (expense) or `credit` (income), and never parcels of a plan or carried-over credits; bank rows by sign. Transfer-like rows (own-account transfers, card bill payments) are not candidates, except payroll (`operation_type = FOLHA_PAGAMENTO`).

Matcher: `match_condition` when set; when null (legacy rows) the existing reconciliation policy (description token overlap ≥ 0.6 under the workspace's policy documents) — legacy rows keep matching what they matched before, and now also history.

Per period, in chronological order (so the expectation evolves with confirmed charges):
1. **Decisions first:** rows with `recurring_manual_id` are linked to the period with the nearest anchor date → `manual`.
2. Each active recurrence opens a slot (expected date + amount). Pairs (row × slot) inside the window are ranked by cost = `|amount − expected| / expected / tolerance` (× 0.5 in variable mode) + `|days| / window` + 0.5 when the row is in another calendar month than the period. Cheapest first, one row per slot → **`auto`** when the amount fits. This is what tells apart several subscriptions at the same merchant (Apple R$ 10,99 / 49,99 / 66,90) and leaves one-off purchases out.
3. Empty slot + a row of the same merchant in the window with the amount outside tolerance → **`conflict`**, issue "amount changed (charged X, expected Y)".
4. **IOF companion:** for foreign-currency recurrences, a BRL row of the same description key, ±1 day, with 2–7 % of the main charge's BRL amount, follows the main charge's link (`recurring_companion = true`).
5. Another fitting row for a slot already filled → **`conflict`**, issue "second charge in the same period — double charge?".

**Review decisions** (user, then recompute):
- **Accept** (on auto/conflict): `recurring_manual_id = rec`, `recurring_manual_excluded = true` — stays linked, not used for the expected amount.
- **Confirm** (on conflict "amount changed"): `recurring_manual_id = rec`, excluded false — the new amount becomes the reference.
- **Reject**: adds the recurrence to `recurring_rejected_ids`, clears a manual link to it — never auto-linked to that recurrence again (manual linking still allowed and removes it from the rejected list).
- **Link manually / "Not a recurring charge" / Undo**.

### 8.5 Target behavior — timeline, forecast and alerts
For each recurrence: periods from start to `open month + 12`. Slot status: `posted`, `conflict`, `expected` (window not over), `late` (date passed but its month/bill is still open — still forecast), `missed` (closed). Each slot reports its effective month (bill month on cards via Phase 4, calendar month on bank accounts).
- **Forecast = `expected` + `late` slots**, computed, not materialized. Replace the projection source of `dashboard_service` and `transaction_calendar_service` (`get_occurrences_in_range` call sites) with these slots for recurrences with `auto_generate = false`; placeholders keep their current path for `auto_generate = true` (no double counting: a period with a placeholder row is `posted`).
- Card recurrences feed `projected_recurring` of the open/future statements.
- Alerts: number of conflicts; last period missed; **"suggest ending"** after 2 consecutive missed periods (action "End recurrence" sets `end_date` to that period).

### 8.6 Target behavior — suggestions
`GET /api/accounts/{id}/recurring-suggestions` analyses the last ~400 days of live, unlinked, non-transfer rows (reference: `recurrence-suggestions.ts`):
- Groups: direction + currency + description key (≥ 3 letters, ≥ 40 % letters — codes like "TRA-5639146-…" excluded, amount ≥ R$ 2).
- **Fixed:** clusters within 2 % (+ R$ 0,05); **variable:** the rest of the group with coefficient of variation ≤ 40 %, tolerance = max deviation + 20 points clamped to 40–80 %.
- Dominant day = day with most rows within ±window (circular month distance); one row per period (closest to that day).
- Requires ≥ 3 of the last 6 months, last occurrence ≤ 45 days ago, ≤ 1.6 rows per period (excludes groceries, food delivery, PIX to people), most recent run with gaps ≤ 2 months.
- **Yearly:** same amount ±5 %, 11–13 months apart, nothing similar between, within 400 days.
- **Weekly** (new): ≥ 6 occurrences in the last 10 weeks, same weekday ±1, amount within 2 % or variable rules.
- Each suggestion is a complete prefilled recurrence (`match_condition = description_key starts_with <key>`, amounts, day, window, start, sample occurrences). Accepting opens the create form; **Dismiss** stores the suggestion key per account so it never comes back (a dismissed fixed cluster is not re-offered as variable).
- **"Make recurring"** on a transaction returns a draft prefilled from it (not allowed for parcels, payments, previous balance).

### 8.7 API
- `GET /api/recurring?account_id=…` (existing list) gains `timeline`, `alerts`, `next` per recurrence; `GET /api/recurring/{id}/timeline`.
- `POST/PATCH/DELETE /api/recurring…` accept the new fields; every change recomputes the account and returns link stats.
- `POST /api/recurring/{id}/end` `{date?}`.
- `GET /api/accounts/{id}/recurring-suggestions`, `POST /api/accounts/{id}/recurring-suggestions/dismiss` `{key}`.
- `GET /api/transactions/{id}/recurring-draft`.
- `POST /api/transactions/{id}/recurring` `{recurring_id: uuid | "none" | null}`; `POST /api/transactions/{id}/recurring/review` `{decision: accept|reject}`; `POST /api/transactions/{id}/recurring/confirm`.

### 8.8 UI
`recurring.tsx`: per recurrence a period timeline (posted/conflict/expected/late/missed), conflicts badge, "suggest ending" banner, edit form with condition builder (reuse the rules editor), amount mode/tolerance/window, frequency incl. "weekly, billed monthly" (weekday + basis + per-week amount, with a live preview "October: 5 × R$ 150 = R$ 750"). Suggestions panel per account with Create/Dismiss. Transaction detail: recurrence chip with link status and Accept/Confirm/Reject/Unlink/"Not recurring". Remove (or hide behind "legacy") the auto-generate toggle for new recurrences.

### 8.9 Import of toledo-finance recurrences
`backend/scripts/import_toledo_recurrences.py` (shipped with this RFC, runnable once Phase 6's schema exists) reads toledo's Postgres directly and creates the recurrences (and optionally the per-transaction decisions) in Securo. See Appendix D. Requirements it relies on: the columns of §8.2, `accounts.external_id` = Pluggy account id, `transactions.external_id` = Pluggy transaction id, `import_ref` unique per workspace, and `recompute_service.recompute_account`.

### 8.10 Acceptance criteria

```gherkin
Feature: Recurrences v2

  Scenario: AC-6.1 Several subscriptions at the same merchant
    Given recurrences "iCloud" R$ 10,99, "Apple One" R$ 49,99, "AppleCare" R$ 66,90, all matching description_key starts_with APPLECOMBILL, monthly around day 12
    And in October charges of 10,99 (day 12), 49,99 (day 13), 66,90 (day 12) and a one-off 199,00 (day 20)
    Then each charge is linked auto to its own recurrence
    And the 199,00 charge is linked to none

  Scenario: AC-6.2 Price change becomes a conflict, confirmation updates the expectation
    Given "Netflix" fixed R$ 39,90 tolerance 2 %
    When November's charge is R$ 44,90 on the expected day
    Then it is linked with conflict "amount changed (charged 44,90, expected 39,90)"
    When the user confirms it
    Then the link is manual and December expects R$ 44,90

  Scenario: AC-6.3 Accept keeps the value out of the expectation
    Given a variable "Electricity" with last confirmed 180, 200, 220
    When the user accepts an exceptional 600 charge
    Then the period is posted
    And the next period expects 200 (mean of 180, 200, 220)

  Scenario: AC-6.4 Reject is permanent for that recurrence
    When the user rejects a charge linked to "Spotify"
    Then later recomputes never link it to "Spotify"
    And the user can still link it manually, which removes the rejection

  Scenario: AC-6.5 Decisions survive sync
    Given a charge accepted for "Gym"
    When Pluggy updates that charge's amount and status
    Then recurring_manual_id and recurring_manual_excluded are unchanged

  Scenario: AC-6.6 Subscription day drift
    Given "Gym" on day 5 whose last 2 charges fell on day 8
    Then the next expected date is the 8th

  Scenario: AC-6.7 Foreign subscription with IOF companion
    Given "ChatGPT" USD 20,00 and on 2026-10-03 a USD 20,00 charge (R$ 108,40) plus a BRL "CHATGPT IOF" R$ 3,79
    Then both rows are linked to the October period, the IOF as companion
    And the period amount is R$ 112,19

  Scenario: AC-6.8 Weekly billed monthly
    Given "Cleaner" weekly_billed_monthly, Tuesdays, R$ 150 per week, billed day 5, basis previous_month
    Then the period of 2026-10-05 expects R$ 750 and the period of 2026-11-05 expects R$ 600
    When the charge on 2026-11-05 is R$ 640 (4 Tuesdays)
    Then after confirmation the per-week value becomes R$ 160

  Scenario: AC-6.9 Weekly and quarterly frequencies link
    Given a weekly recurrence every Friday R$ 30 window 2
    And a quarterly recurrence day 10 starting January
    Then charges each Friday ±2 days link to consecutive weekly periods
    And charges on April 11 and July 9 link to the April and July periods

  Scenario: AC-6.10 Forecast is computed, not materialized
    Given a recurrence with auto_generate false
    Then no placeholder transaction is created for future periods
    And the dashboard and the transaction calendar show its expected and late periods
    And a period fulfilled by a linked charge is not forecast again

  Scenario: AC-6.11 Late vs missed on a card
    Given a card recurrence expected on 2026-10-10 in the bill closing 2026-10-25
    When today is 2026-10-17 and nothing matched
    Then the period is late and still counted in the open statement's projected_recurring
    When the bill closed with nothing matched
    Then the period is missed

  Scenario: AC-6.12 Suggest ending after two misses
    Given a recurrence missed in September and October
    Then it shows "suggest ending" and "End recurrence" sets end_date to October

  Scenario: AC-6.13 Suggestions
    Given "SPOTIFY" charges of R$ 21,90 on day ~7 in each of the last 6 months
    And "IFOOD" charges 12 times a month
    Then a fixed monthly suggestion for SPOTIFY exists and none for IFOOD
    When the user dismisses the SPOTIFY suggestion
    Then it never appears again for that account

  Scenario: AC-6.14 Legacy recurrences keep working
    Given a recurrence created before this change (no match_condition, exact amount)
    Then charges with that exact amount and similar description link as before
    And its next_occurrence keeps advancing

  Scenario: AC-6.15 Parcels and payments are never recurring
    Then "Make recurring" on a parcel, a bill payment or a previous-balance row is refused

  Scenario: AC-6.16 Import script
    Given toledo-finance has 12 recurrences on accounts that exist in Securo by Pluggy account id
    When the import script runs with --apply
    Then 12 recurrences exist with origin import and import_ref "toledo:<id>"
    When it runs again
    Then nothing is duplicated (existing ones are updated)
```

### 8.11 Tests
Pure: periods per frequency (incl. clamping, weekend adjustment, weekly_billed_monthly units), expectation evolution (fixed/variable/excluded/FX), slot assignment (costs, multi-subscription, conflict kinds, companion), timeline statuses, suggestions (fixed/variable/yearly/weekly, exclusion heuristics, dismiss). Service: decisions vs sync, legacy matcher regression, dashboard/calendar projection without double counting, import script against fixture DBs.

---

## Appendix A — Pluggy → Securo field map

| Pluggy | Securo column | Phase |
|---|---|---|
| `Transaction.id` | `transactions.external_id` | existing |
| `providerId` | `provider_id` | 2 |
| `providerCode` | `provider_code` | 2 |
| `description` / `descriptionRaw` | `description`+`original_description` / `provider_description_raw` | existing / 2 |
| `amount`, `currencyCode`, `type`, `status`, `date` | existing columns (now always updated) | 2 |
| `amountInAccountCurrency` | `amount_in_account_currency` (+ existing `amount_primary`) | 2 |
| `balance` | `balance_after` | 2 |
| `category`, `categoryId` | `provider_category`, `provider_category_id` | 2 |
| `operationType` | `operation_type` | 2 |
| `merchant.{name,businessName,cnpj}` | `merchant_name`, `merchant_cnpj` | 2 |
| `paymentData` | `payment_data` | 2 |
| `creditCardMetadata.installmentNumber/totalInstallments/totalAmount/purchaseDate` | existing installment columns | existing |
| `creditCardMetadata.paymentType` | `payment_type` | 2 |
| `creditCardMetadata.cardNumber` | `card_number` | 2 |
| `creditCardMetadata.payeeMCC` | `merchant_mcc` | 2 |
| `creditCardMetadata.billId` | `provider_bill_external_id` (+ `bill_id` FK when stored) | 2 |
| `creditCardMetadata.billForecastDate` / `billPostDate` | `bill_forecast_month` / `bill_post_date` | 2 |
| `creditCardMetadata.transactionDateTime` / `purchaseDate` timestamp | `transaction_at` | 2 |
| `createdAt` / `updatedAt` | `provider_created_at` / `provider_updated_at` | 2 |
| `Account.creditData.{availableCreditLimit,balanceCloseDate,balanceDueDate,holderType}` | `available_credit_limit`, `balance_close_date`, `balance_due_date`, `card_holder_type` | 2 |
| `Account.bankData.{overdraftContractedLimit,overdraftUsedLimit}` | `overdraft_limit`, `overdraft_used` | 2 |
| `Bill.{closingDate?,allowsInstallments,payments,financeCharges}` | `closing_date`, `allows_installments`, `payments`, `finance_charges_total` (+ existing synthetic rows) | 2 |
| `Item.{status,executionStatus,lastUpdatedAt,consentExpiresAt}` | `bank_connections.provider_*`, `consent_expires_at` | 2 |

## Appendix B — toledo → Securo condition mapping (used by the import script)

| toledo field | Securo field | | toledo op | Securo op |
|---|---|---|---|---|
| `description` | `description` | | `eq` / `neq` | `equals` / `not_equals` |
| `descriptionKey` | `description_key` | | `contains` / `notContains` | `contains` / `not_contains` |
| `descriptionRaw` | `provider_description_raw` | | `startsWith` | `starts_with` |
| `merchantName` | `merchant_name` | | `matches` / `notMatches` | `regex` / `not_regex` |
| `amount` | `amount` | | `in` / `notIn` | `in` / `not_in` |
| `signedAmount` | `signed_amount` | | `isNull` / `isNotNull` | `is_empty` / `is_not_empty` |
| `type` (DEBIT/CREDIT) | `type` (debit/credit) | | `lt` `lte` `gt` `gte` | same |
| `status` (PENDING/POSTED) | `status` (pending/posted) | | `{and:[…]}` / `{or:[…]}` | group `{op, conditions}` |
| `operationType` | `operation_type` | | `{not: …}` | `{not: …}` |
| `category` | `provider_category` | | | |
| `currencyCode` | `currency` | | | |
| `date`, `cardNumber`, `installmentNumber`, `totalInstallments`, `paymentType` | `date`, `card_number`, `installment_number`, `total_installments`, `payment_type` | | | |

## Appendix C — Bank behaviors observed in production data (Open Finance via Pluggy)

| Bank | Behavior | Handled by |
|---|---|---|
| Sicredi | Returns the purchase twice with different ids: first PENDING without bill (recent endpoint), later POSTED with bill; Pluggy never removes the pending one; sometimes 2+ pending copies | §4.7 duplicates |
| Sicredi | Parcel marker prefix `PARC=1NN` (1 + number of parcels) | `br.marker_parc_eq` |
| Sicredi, CAIXA | Parcel N is only posted when its bill closes | §7 projection, `createdAtFrom` sweep |
| CAIXA | Previous balance as a row `TOTAL DA FATURA ANTERIOR`; full ledger per bill | `br.previous_balance`, ledger total |
| CAIXA | Bill payment only as text `PGTO.BOLETO`/`PAGTO.BOLETO`/`OBRIGADO PELO PAGAMENTO` | `br.payment_by_text` |
| CAIXA | Re-sends credits of bill N−1 in bill N (same date/amount, text may change) | carried-over credits |
| CAIXA | Parcel marker `NN DE NN` | `br.marker_de` |
| Pluggy (since 2026-10-15) | Synthesized "Pagamento recebido" PAGAMENTO_FATURA linked to the paid bill | payment echoes |
| Several | Parcel cents: first parcel absorbs rounding (Kiwify 12x: 7,03 + 11 × 6,92) | tolerance N+1 cents |

## Appendix D — Recurrence import script

```
# inside the backend container (or any env with the backend deps and access to both databases)
python scripts/import_toledo_recurrences.py \
  --toledo-url postgresql://postgres:postgres@<host>:<port>/toledo_finance \
  [--workspace-id <uuid>] [--with-decisions] [--apply]
```
- Default is a **dry run** printing what would be created/updated/skipped; `--apply` writes in one transaction.
- `--toledo-url` (or env `TOLEDO_DATABASE_URL`). toledo's compose does not publish Postgres on the host: temporarily add `ports: ["127.0.0.1:5433:5432"]` to its `db` service, or attach the Securo backend container to toledo's network (`docker network connect toledo-finance_backend <securo-backend-container>` and use host `db`).
- Accounts are matched by Pluggy account id (`accounts.external_id` of a Pluggy connection). Recurrences whose account is not connected in Securo are skipped and listed. `--workspace-id` restricts to one workspace when the same Pluggy account exists in several.
- Field mapping: name → `description`; EXPENSE/INCOME → `debit`/`credit`; `expected_amount` → `amount`; FIXED/VARIABLE → `amount_mode`; `tolerance_pct`, `day_window`, `currency_code`; MONTHLY/YEARLY → `monthly`/`yearly`; `start_month` + `day_of_month` → `start_date`; `end_month` → `end_date`; `enabled` → `is_active`; condition converted with Appendix B (unsupported → recurrence skipped with the reason); `origin = import`, `import_ref = toledo:<id>`, `auto_generate = false`.
- `--with-decisions` also copies per-transaction decisions (manual link, "not recurring", accepted/excluded, rejected list), matching transactions by Pluggy transaction id; unmatched ones (ids changed after a reconnect) are counted and listed.
- Idempotent by `import_ref`; after `--apply` it recomputes each affected account.

## Appendix E — Reference implementation map (toledo-finance)

| Topic | File |
|---|---|
| Credential crypto (AES-256-GCM, AAD, hint) | `apps/api/src/crypto.ts`, `apps/api/src/services/credentials.ts` |
| Pluggy client per credential | `apps/api/src/pluggy/client.ts` |
| Field mapping | `apps/api/src/pluggy/mappers.ts` |
| Sweeps (date window + createdAt watermark) | `apps/api/src/sync/sync-item.ts` |
| Upsert of provider fields | `apps/api/src/sync/upsert.ts` |
| Removal by absence + guard, missing bills | `apps/api/src/sync/finalize.ts` |
| Recompute pipeline | `apps/api/src/sync/recompute.ts` |
| Reference month + calibration | `apps/api/src/domain/reference-month.ts`, `domain/billing-cycle.ts` |
| Duplicates | `apps/api/src/domain/duplicates.ts` |
| Statements, carried credits, payment echoes | `apps/api/src/domain/card-statement.ts`, `routes/statements.ts` |
| Installments | `apps/api/src/domain/installments.ts`, `sync/installment-plans.ts`, `domain/projection.ts` |
| Recurrences | `apps/api/src/domain/recurrences.ts`, `services/recurrences.ts`, `routes/recurrences.ts` |
| Suggestions | `apps/api/src/domain/recurrence-suggestions.ts` |
| Rules | `apps/api/src/domain/rules/{types,engine,fields,seeds}.ts`, `domain/classification.ts`, `services/classification.ts` |
| Tests worth porting as fixtures | `apps/api/test/*.test.ts`, `apps/api/src/domain/*.test.ts` |
