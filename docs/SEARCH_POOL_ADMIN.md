# Owner search pool

`plagiarism.search_pool_admin.PoolAdmin` stores immutable search credentials and
serves safe metadata to the separate administrator dashboard. It does not choose
the visitor's provider, run searches, create grants or change the site's180 limit.

## Integration

Main owns `cloud/web_beta.py`, `server.py`, deployment manifests and runtime
adapters. This branch deliberately does not edit those files.

```python
from plagiarism.search_pool_admin import PoolAdmin, route as pool_route

beta.pool_admin = PoolAdmin(
    beta.root, beta.persist,
    runtime_status=pool_metrics,
    activation_policy=existing_grant_admission,
)
# cloud.dashboard owns require_admin and invokes the hook after authenticating.
dashboard_runtime.pool_route = pool_route
# Hook invocation: pool_route(handler, method, path, body, authorize=require_admin)
```

The hook calls the supplied `authorize(handler, method)` before accessing
`handler.server.beta.pool_admin`. No supplied guard means401. The guard must
authenticate the **separate dashboard cookie** and validate exact Host/Origin,
Fetch Metadata and `X-CSRF-Token` on mutations. User/API/owner tokens are not admin
credentials. Main must gate before reading POST bodies as well. Reusing the guard
for the hook is harmless and ensures direct route invocations also default-deny.

Serve `/dashboard/pool` from `web/pool.html` and `/pool.js`, `/pool.css` only after
the same administrator gate. Anonymous HTML requests redirect to
`/dashboard?next=pool`. Session API is `GET /api/dashboard/session`, returning
`{authenticated:true,username,csrf_token,expires_at}`. UI keeps CSRF only in memory,
clears the password field on submit and never uses browser storage. Serve JSON and
assets with `Cache-Control: no-store`, `X-Robots-Tag: noindex, nofollow`, nosniff,
no-referrer and a CSP permitting only same-origin script/style/connect with
`frame-ancestors 'none'; base-uri 'none'; form-action 'self'`.

The runtime must **explicitly use the vault**. Existing `search_credentials.key_for`
prefers environment variables, so saving a vault key alone does not rotate an old
environment key. Do not mutate process environment during jobs. Instead, capture
`runtime_snapshot()` when admitting a job, pin provider+credential ID+revision,
resolve that exact ID internally, and pass it explicitly into the adapter. Existing
queued/running jobs keep their previous IDs. Disable affects future snapshots.

`runtime_snapshot()` returns committed configuration, not permission to spend:

```python
{"revision": 4, "active": {"serpapi": {
    "credential_id": "opaque32hex", "account_ref": "serpapi:hmac-or-null",
    "verification": {"code": "free_verified", "checked_at": 1700000000,
                     "provider_remaining": 230, "balance_source": "provider_live",
                     "free_verified": True, "guard_remaining": 230,
                     "plan_limit": 250, "plan_used": 20},
    "admission": None
}}}
```

Old observations can be stale. Main independently checks current free policy and
grant ledger before every search and never admits paid overage. Do not treat a
snapshot, UI health label or a previous quota check as reusable spending authority.
`resolve_credential(id)` is server-only and returns the raw key; never expose it in
an HTTP route, log, exception or serializer. SerpApi account IDs are HMAC-bound;
key fingerprints identify credentials, not accounts. Tavily usage has no stable
account identity, so retain the provider/account ledger across all key rotations.

## HTTP contract

| Method/path | Exact JSON fields | Response |
|---|---|---|
| GET `/api/dashboard/pool` | none | Safe status |
| POST `/api/dashboard/pool/stage` | `provider`, `api_key` | `{credential_id,pool}` |
| POST `/api/dashboard/pool/validate` | `credential_id` | `{credential_id,pool}` |
| POST `/api/dashboard/pool/activate` | `credential_id` | `{credential_id,pool}` |
| POST `/api/dashboard/pool/disable` | `provider` | `{pool}` |

Providers are exactly `serpapi,tavily,serper,exa`; the runtime maps its Yandex alias
to canonical `serpapi` for account quotas. Body cap4096bytes, key16..1024printable
ASCII/no whitespace, Tavily `tvly-` prefix, IDs32lowerhex. Extra fields rejected.
All writes require JSON and POST. Errors use existing `{error,code}` envelope and
fixed Russian messages. No raw provider errors or responses leave the service.

Status has `revision,recovery_required,runtime_connected,monthly_limit:180,
monthly_used,providers[],credentials[],audit_count`. Unknown values are `null`.
Credentials show only ID, HMAC display fingerprint, last4, activation state,
bounded numeric quota fields, last check/freshness and safe policy code. Provider
rows distinguish `balance_source:provider_live|unconfirmed` and `provider_remaining`
from `local_used/local_remaining`, grant observations and health/low-quota flags.
`runtime_connected` only indicates a successfully read metrics callback; runtime
adapter integration still needs the explicit snapshot/resolver wiring above.

## Live quotas and bounded free grants

The default probe makes only read-only requests to documented fixed HTTPS hosts:
[SerpApi account](https://serpapi.com/account-api) and
[Tavily usage](https://docs.tavily.com/documentation/api-reference/endpoint/usage).
No redirect, timeout12seconds, response cap64KiB, no automatic retries. The current
search key authenticates each request. Check cooldown3seconds; admin activation
requires a successful observation no older than300seconds and not future-dated.

SerpApi must be Active, Free Plan,250/month, price0, extra0 and consistent integer
counters. Tavily must be Researcher1000, paygo usage+limit0; guard900 and any tighter
key cap apply. A paid plan or unconfirmed response cannot activate.

Serper and Exa do **not** have a live balance in this interface. `validate` reports
`balance_unconfirmed` without any provider call. An Exa search key is not a team
admin/service key. No private admin endpoint is called with it.

`activation_policy(candidate)` is a pure trusted server callback. It looks up an
**existing** approved, account-bound free grant and explicit credential approval;
it never creates a grant. Candidate contains
`{provider,credential_id,account_ref,verification,revision}`. Required evidence:

```python
{"approved": True, "provider": candidate["provider"],
 "credential_id": candidate["credential_id"],
 "account_ref": "trusted-opaque-account", "grant_id": "existing-ledger-grant",
 "remaining": 42, "expires_at": 1700001000,
 "cash_spend_limit_usd": 0, "max_unit_cost_usd": 0.004}  # Exa; Serper uses0
```

Missing/boolean/malformed counters, expired grant, mismatched provider/credential,
nonzero cash limit or wrong unit cost deny activation. Exa's$0.004 is the maximum
debit from an already confirmed free credit grant per call, with cash spend$0.
New keys of the same account retain the same grant ID and spend history. New
accounts do not authorize bypassing a provider's free policy. Grant/account IDs
remain private; UI shows only approved numeric budget observations. Main's
search-time ledger is authoritative after activation and across restarts.

`runtime_status()` supplies allowlisted metrics only:

```python
{"monthly_limit": 180, "monthly_used": 26, "providers": {
 "serpapi": {"local_used": 26, "local_remaining": 154,
             "grant_remaining": None, "grant_expires_at": None,
             "grant_approved": False}}}
```

The service fixes monthly limit180 regardless of callback value. Missing counters
stay unknown; neither staging nor rotation changes them. Do not return raw errors,
keys or arbitrary strings from callbacks. Only numeric/bool fields are serialized.

## Durability and recovery

Files under `<root>/search-pool-vault/` are private600, directory700. POSIX `flock`
serializes readers and writers; use one active volume writer. Atomic replace,
file+directory fsync and a write-ahead rollback intent preserve the last committed
state. Every mutation writes a safe audit entry and invokes noargs `persist()`
at each durability barrier before success. It must synchronously commit the same
volume and must not have runtime activation side effects.

A failed barrier retains/restores `pending.json`; all readers select the prior
state, `runtime_snapshot()` refuses new jobs and pinned old IDs still resolve.
Restart does not silently publish the uncertain candidate. An operator can call
`pool_admin.vault.recover()` on the server to restore/persist the prior state and
clear the intent; no browser recovery endpoint exists. Recovery failures remain
closed. Backup/restore the entire vault together, never individual files.

This is local/volume durability, not a distributed transaction across arbitrary
remote systems. A callback that commits externally and then raises has ambiguous
remote durability; reconcile the volume before serving a replacement instance.
Filesystem writes failing so completely that no journal can be restored also
require operator storage repair; the affected instance refuses further reads.

Versions and audit are not truncated or reset. Capacity is128versions,
10000events/2MB; exhaustion blocks new writes. Retired versions need explicit
server maintenance only after main proves no job references them.

## Verification

`python3 -m unittest discover -s tests -p 'test_search_pool_admin.py' -v` runs
synthetic service, fault-injection, restart, concurrency, bounded transport and
actual HTTP adapter checks. The HTTP harness has a synthetic guard; main must run
the integrated production dashboard-auth suite as well. Browser smoke uses the
same real service and synthetic provider boundary; no real keys/accounts are read.
