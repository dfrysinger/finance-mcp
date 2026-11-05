# Read-only Crew pocket access

Design status: `approved`. Design cycle: `1`. Revision: `crew-design-003`.
Lane: `critical`, because this adds banking authentication and session storage.
Reframe status: `CLEAR`. The independent challenge and design review permit
the native fixed-read route; no observed revisit condition blocks
implementation. Real-account acceptance A7 remains unproven.

## Objective

Let Copilot read Crew pocket balances and pocket activity, including internal
transfers, through finance-mcp without changing Crew records or existing
aggregate financial reports.

## User and scope

The user organizes Crew money into pockets. Copilot needs balances and pocket
activity, including internal transfers, without asking the user to transcribe
them. The supported caller is the existing personal finance MCP client.

The user requested implementation with design-doc, development-loop, and
unattended-run, then accepted "read only is fine for now but we might want to
go farter later". The exact decision record and pre-proposal evidence packet
are supplied with the review handoff, revision `crew-pockets-evidence-001`.
Future write support is not a current grant.

This version does not move money, edit Crew records, calculate reimbursements,
change envelopes or categories, replace SimpleFIN, add historical balance
snapshots, add an automatic refresh, deploy another app, or expose arbitrary
provider operations. The existing web UI and scheduler are unchanged.

## Evidence and constraints

Direct inspection of SimpleFIN's Crew response found a combined account but
no pockets or internal pocket transfers. A new upstream read is necessary.
The [official individual API](https://docs.trycrew.com/#user-authentication)
supports OTP login and GraphQL at
`https://api.trycrew.com/willow/graphql`.

The [unofficial reference client](https://github.com/bemeek-io/go-crew)
demonstrates account/subaccount reads and captures rotating response tokens.
Its [validation notes](https://github.com/bemeek-io/go-crew/blob/main/docs/api.md)
report account and transaction validation, but not live transfer validation.
These reports do not prove compatibility with the user's account.

The current official schema names `Transfer.subaccountFrom`,
`subaccountTo`, `accountFrom`, `accountTo`, `amount`, `occurredAt`, and `status`.
It also exposes `Account.cashTransactions(searchFilters: ...)` with nested
`subaccount` and `transfer`. These are the intended read paths. No banking
mutation or integrator enrollment is needed by the documented route.

| Constraint | Provenance and protected outcome | Revisit condition |
|---|---|---|
| Expose balances and activity, including internal transfers | Direct user decision; this is the missing capability | Live consumer reads cannot identify the required movement |
| No banking writes or arbitrary GraphQL | User's accepted initial scope; prevents the model adding ungranted operations | User deliberately grants a separate write capability |
| User controls real-account login input | Protected real-account input boundary; the agent must not obtain codes | A supported attended login facility can carry this same operation with less machinery |
| Keep pocket data out of aggregate accounts and transactions | Pockets partition money already present in SimpleFIN; prevents duplicate assets/spending | Explicitly replacing the combined-account source in separately designed accounting work |
| Existing Python MCP remains the product boundary | Current package and accepted proposal; avoids another service/runtime | A documented consumer capability cannot be carried by this boundary |
| Preserve the latest session after response rotation | Reference client behavior; avoids using an invalid predecessor token | Provider publishes a different session protocol |
| Serialize Crew session use across entry points | CLI reads/login/disconnect and MCP reads are distinct process callers | Provider establishes safe independent sessions or a single existing owner takes over |
| Private storage follows `FINANCE_MCP_HOME` | Existing credential owner in `config.py`; prevents new synced project credentials | An approved credential owner replaces this file |

No external policy limiting deliberate owner grants has been evidenced.
These controls constrain this connector's approved operations, not the
user's freedom to authorize a different feature. They do not isolate secrets
from arbitrary programs running as the same operating-system user.

## Minimum design and reuse contract

Add a small stdlib-only `crew.py` provider module. Reuse `config.home_dir()`,
the argparse CLI, FastMCP registration, existing structured error conventions,
and pytest. Add no package dependency, service, webhook, generic provider
framework, browser automation, or authentication broker.

Use **live reads**, not another cache or archive namespace. Pocket data has no
historical storage requirement in this scope. Returning Crew's source pages
directly is smaller than adding sync integration, a second ledger, invalidation,
and stale-cache recovery. Existing `sync.py` has no cross-process serialization;
it remains untouched because Crew is not added to its refresh callers.

The independent pre-proposal minimum recommended reusing sync and separate
storage. This proposal removes that machinery while preserving the underlying
properties: fresh source reads, no duplicate accounting, and explicit failures.
It requires the challenger's current pass-2 comparison before normal review.

New persistent state is only `crew_session.json` and a stable `crew.lock`
inside the private finance home. The session contains the fully authenticated
token and a usability marker, with no phone, email, OTP, response body, or financial cache. Locking
protects the real CLI and MCP callers, not hypothetical replicas.

### Connection points and authority

- `config.py`: private Crew session and lock paths.
- `crew.py`: fixed HTTPS/auth requests, response validation, token persistence,
  serialization, and typed read functions. It owns no archive connection.
- `cli.py`: `crew login`, `crew status`, `crew disconnect`, and matching
  pocket/activity/transfer reads.
- `server.py`: the three named read tools below. No login, disconnect, token,
  URL, query-text, HTTP-header, or banking-write argument is exposed.
- `README.md`: how to run attended login and use live reads.
- `tests/test_crew.py`, existing CLI/server tests, and the invariant register:
  direct behavior and accounting-boundary checks.

The model may select supported read parameters. It cannot change the endpoint,
query shape, authentication flow, or operation type through the MCP interface.
Deterministic application code owns those choices. The provider remains
responsible for authorizing which account IDs the session may read.

## Authentication and session lifecycle

`finance-mcp crew login` runs only in an attended terminal. Refuse non-TTY
input before sending an OTP. Read phone/email when needed and OTPs through
hidden terminal input, never flags, environment variables, MCP, or chat.
Do not write those values or raw auth responses to logs. The agent does not
run this command against the real account on the user's behalf.

Use the four fixed `/willow/auth/...` paths shown in the official examples and
reference client. The docs' `/willow/graphql/auth` headings disagree; do not
probe both routes or send duplicate OTP requests automatically.
Keep intermediate MFA tokens in memory. Publish a credential only after
the required verification completes. A later fixed pocket query verifies
data access; saving a credential alone does not establish a working connector.

For each operation, open a fresh lock-file descriptor and acquire one
nonblocking exclusive OS file lock before loading a saved token;
hold it through the request and saving any rotated token. Login and disconnect
take the same lock. Never reuse a cached lock-file descriptor, including for
calls in the same process. A contending caller receives `busy` without making
a request; this is neither an authentication failure nor an empty data result.
The lock file stays in place, avoiding multiple lock identities.

Create session files with mode 0600 before writing, in a 0700 directory;
write a sibling temporary file and atomically replace the session. Do not copy
`save_access_url`'s write-then-chmod pattern. Failure to establish private modes,
acquire the lock, or persist rotation stops the operation with a redacted error.
Capture a response token before decoding its body, including error responses.
Do not start a later request with the old token after a failed save.
Before a saved token is sent, atomically mark the session unusable. Receiving
and saving response headers restores usability, even if the body later fails.
An interrupted request or failed rotation save leaves it unusable across
processes and requires attended sign-in; a 401 also leaves it unusable.

Disable HTTP redirects for authenticated requests. Use fixed HTTPS endpoints
and certificate verification; do not forward authorization to redirects or
accept caller-specified hosts. Do not log request/response bodies or headers.
Expose bounded error codes and fixed guidance, not raw provider exception text.
A 401 requires attended re-login; other provider/schema failures do not falsely
claim that the session is valid or the pockets are empty.

`crew status` checks local credential presence with file metadata only,
without acquiring the lock or opening/reading session contents. It never
reports token contents or an inferred authenticated state. `crew disconnect`
removes the local session;
it does not claim to revoke the upstream session or change the bank account.
Interrupted login, lost responses during rotation, and a process crash can
require re-login; this design does not promise unattended session renewal.

## Read contract

All three tools make live requests and report `source: crew_api` and
`retrieved_at`. This timestamp means when the response was received, not when
the bank settled or refreshed a balance. Return a provider balance timestamp
separately where available. There is no stale-data fallback.

| Tool | Fixed query and returned data |
|---|---|
| `crew_pockets()` | `currentUser.accounts`: account identity/type, cleared and overall balances, and subaccounts with identity/name/type and cleared/overall balances. Request all documented subaccount types explicitly because the default list excludes bill/reserve types. Keep null balances distinct from zero. |
| `crew_pocket_activity(account_id, pocket_id, after=None)` | Account's `cashTransactions`, using variables and `searchFilters.subaccountId`: ID, signed integer-cent amount, title/memo, status/type, `occurredAt`, nullable `clearedAt`, pocket identity, and associated transfer identity/endpoints. Never interpolate caller text into GraphQL. |
| `crew_transfers(after=None)` | `currentUser.transfers`: source IDs/names for both account and subaccount endpoints, ID, integer-cent amount, status/type, memo, and `occurredAt`. Preserve external or missing endpoints rather than inventing a pocket identity. |

Use forward Relay pagination only. One activity/transfer call returns one
provider page with `has_next_page`, `end_cursor`, and `coverage: page`.
An empty page and a completed connection are distinct. Never claim the page is
the user's complete history; the caller follows cursors to inspect older data.
Return a usable changed cursor when there is another page, or a protocol error.
Stable source IDs let the caller identify repeated rows between pages.

On lock contention, return `ok: false`, `error_code: busy`, and fixed
retry-later guidance without making an HTTP request or claiming fresh data.

The page size and request timeout are ordinary implementation defaults informed
by the reference client's working read shape, not product or security limits.
Choose them in implementation, document them, and revisit them on observed API
rejection or unacceptable response time. Do not build an unbounded page walker.

Validate the expected object/connection shape. A missing `currentUser`,
unrecognized node, null connection, invalid integer amount, GraphQL errors
including partial `data`, or malformed pagination fails explicitly.
A source-successful empty list is valid. Nullable amounts remain unknown,
not zero; booleans are not integer-cent amounts.

Transfer direction comes from named endpoints, not the sign of a guessed
reimbursement. A transfer with unknown endpoints remains explicitly
unattributed. Do not infer completion from presence alone: preserve pending,
canceled, failed, and completed status. If the live API omits internal
movements, keep the feature incomplete and reopen the route decision rather
than silently dropping history.

### Example journey

The user runs attended login once. Copilot calls `crew_pockets` to find a
specific pocket and its parent account, then `crew_pocket_activity` for that
pair. For money moved between pockets, Copilot calls `crew_transfers` and
follows `end_cursor` if more pages exist. It receives source amounts, time,
status, and both endpoints where provided. Existing net-worth and spending
tools still read only the original SimpleFIN archive.

## Failure model and observable acceptance

| ID | Required behavior | Failure that must remain visible |
|---|---|---|
| A1 | Pocket reads distinguish source success, null amounts, empty data, and `busy` | Schema/auth/network/lock failures must not become an empty successful list |
| A2 | Activity and transfers retain IDs, amounts, direction, time, status, and page coverage | A first page, missing endpoint, or pending transfer must not imply complete settled history |
| A3 | The exposed interface issues only the fixed approved read operations | Arbitrary query/host inputs or bank mutations must have no invocation path |
| A4 | Real login input remains user-controlled and sessions private | Non-TTY input, debug errors, loose files, and partial MFA must not expose or publish credentials |
| A5 | Concurrent callers, including same-process calls, acquire fresh lock descriptors and cannot overwrite newer tokens or use stale in-memory sessions | Busy lock, storage failure, and response rotation must stop or persist in the specified order |
| A6 | Crew reads do not change existing aggregate accounting or configuration | No new assets, transactions, category edits, schedules, or envelope mappings |
| A7 | Delivered MCP behavior works for the real Crew account | Fixtures alone cannot prove current login, visible-pocket coverage, or identifiable internal transfers |

## Deterministic check contract

Write focused tests with implementation; no executable pre-build guard is
needed. Use synthetic identities, tokens, balances, and OTPs only.

| Check | Setup and expected result | Why failure proves the contract |
|---|---|---|
| C1 -> A1 | Mock successful pocket responses with integer/null balances and an empty account list; separately inject missing keys, GraphQL partial errors, invalid amounts, 401, and network failure. Only complete source success returns `ok: true`. | Separates no data from inability to obtain it. |
| C2 -> A2 | Two forward pages, a final empty page, missing/repeated next cursor, signed activity amounts, transfer with both endpoints, null endpoint, and pending/canceled status. Assert source fields and `coverage: page`; errors cannot claim completeness. | Detects lost direction/status and truncated-history claims without crawling a real account. |
| C3 -> A3 | Capture HTTP requests from every public Crew entry point. Attempt query-like IDs/cursors and an authenticated redirect. Assert constant query documents with variables, fixed destinations, and no redirect request or banking mutation. Inspect MCP tool schemas for absence of credential/query/URL inputs. | Exercises model-controlled inputs at the real authority boundary rather than checking prose. |
| C4 -> A4 | Synthetic terminal prompts exercise single-factor and email flows, failure/interruption, and non-TTY refusal. Inspect files/modes, argv, captured output, and redacted errors with secret sentinels. Failed MFA publishes no new session; no secret sentinel appears in output. | Proves the supported login flow and storage avoid the stated exposure paths. |
| C5 -> A1,A5 | Two processes and two overlapping same-process calls contend on the same lock, opening fresh descriptors each time. Rotate a token, including on an HTTP/body error, and issue the next read. Inject persistence failure. Assert the loser reports `busy` and sends no request, next read loads the newest saved token, and failed save prevents continuation. Local status checks never read the token or block on this lock. | Reproduces the actual conflicting operations and credential-loss path. |
| C6 -> A6 | Seed the existing archive/config, run Crew tools against fixtures, and compare the files and aggregate reports before/after. Exercise SimpleFIN regression tests. | A pocket inserted as another account or transaction changes the actual protected outputs. |
| C7 -> A7 | Through the actual installed stdio MCP consumer, inspect tool discovery and synthetic refusal/success behavior, then run authenticated reads after user sign-in. Privately match visible pockets, a known activity entry in a specific pocket through `crew_pocket_activity` (parent account/pocket identity, source ID, amount, time, and status), and a known existing internal transfer's amount, endpoints, time, and status. Compare against an independent user-visible Crew record or user confirmation, not only another query using the same filter. Inspect older pages if present. If no suitable pocket activity can be matched, keep A7 open. | Proves source-to-runtime correspondence, live pocket-filter behavior, and transfer coverage; no real financial mutation is needed. |

C7's authenticated portion is a human-input gate, not permission to obtain OTPs.
Record only redacted pass/fail and coverage in committed proof. Missing live
evidence keeps A7 open; it does not prevent building the reviewed connector.
Once the design review closes, `CLEAR` means no observed architecture-revisit
condition blocks the reviewed implementation. It does not mean A7 passed.
The user may run the attended login; only after that succeeds may the agent
exercise the approved fixed real-account reads. No agent-initiated OTP request
or agent access to protected input is permitted. Observed live incompatibility
reopens the design; simply awaiting that proof is a delivery gate.

## Reframe gate, migration, and rollback

The current blocked outcome is pocket visibility, not refresh orchestration.
The binding constraints are the user-selected read scope, protected input,
and preserved aggregate accounting. Removing a new cache retains these
invariants and reduces trusted components. The remaining minimum is a native
client, private session lifecycle, and fixed read tools.

Reopen this design before adding a service, new store, automatic refresh,
broker, endpoint fallback, integrator grant, mutation, or accounting behavior.
Also reopen it if login or internal-history fields cannot satisfy A7, the
provider's bill/reserve filtering hides required visible pockets, or repeated
repairs only move the failure to another internal boundary. Compare the
simplest alternative against the whole route, not only its newest leaf.

No archive migration occurs. Rollback removes Crew tool registration and CLI
commands with the feature commit, then uses local disconnect/removes only the
Crew session. Leave the existing SimpleFIN credential, archive, budget, and
scheduler unchanged. Never claim local deletion revokes Crew's session.

Fail-closed evidence is C3-C5: unsupported inputs cannot select operations,
noninteractive login sends no OTP, redirects receive no credential, failed MFA
publishes no credential, and failed token persistence cannot continue a read.
This is a fixed application boundary, not a promise of OS-level isolation.

## Definition of Done: Read-only Crew pocket access

- The current independent constraint challenge permits the route, both design
  review families complete, and the reframe status is `CLEAR`.
- A1-A7 have the named evidence, with live compatibility verified after the
  user completes protected sign-in.
- The installed MCP exposes the reviewed read-only behavior, and reviewed
  source corresponds to that runtime.
- Existing accounting and SimpleFIN behavior remain unchanged; no banking
  writes or private credentials/data enter the repository.
- Implementation proof and reviews pass; the owned branch is published through
  the repository's normal PR path and its installed runtime is verified.
