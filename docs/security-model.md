# Security model and limits

The model proposes structured plans; application code retains execution authority. Tool output, page titles/snippets, memory and summary are treated as data rather than additional permission. This is a design boundary with tested checks, not a claim that prompt injection is solved.

## Tested invariants

- Catalog validates current tool name, schema and revision before dispatch. Provider adapters retain their own allowlists, bindings, readiness checks and explicit-command requirements.
- Request IDs and durable reservations prevent blind replay through cooperating paths. A different payload under an existing ID conflicts.
- PREPARED/ATTEMPTED/ACCEPTED/RESULT_CONFIRMED are distinct. An SDK receipt, observed switch/state, and a person's physical confirmation are not interchangeable.
- Unknown effect is held and queried using its existing ID. It is not silently marked successful, cleared, or routed to a second provider after an ambiguous write.
- Executor/task fences and a local dispatch lock reject stale cooperating owners. Stops/corrections are stored independently of summary.
- Proactive suggestions and persistent quotas are separate from permission to change a device or start a service. A proposal does not execute itself.

## Scope of protection

A same-user process can access that user's files and credentials; private IPC/0600 files do not create an OS sandbox. Optional account-separated Core/operator bridge supports a narrower account boundary, but it requires an independently reviewed administrator deployment. Registry owns bounded Docker execution; Core does not need direct Docker control in that deployment. The offline preview does not grant or install those privileges.

The state database is not encrypted at rest by this code. Malicious local users with sufficient privileges, compromised dependencies, bypass writers, provider replay behavior, false/stale device reports and hardware failures are outside an exactly-once claim. Some legacy opaque commands conservatively remain UNKNOWN. No distributed transaction across the ledger and physical device is implemented.

Browser results may provide title/URL/snippet without page content; content_read=false must not be described as reading the page. Models can still produce unsupported claims despite prompts. Memory and Gate availability flags are not authenticated facts.

The public preview omits credentials, model/voice assets and operational state. Do not post a raw `.env`, token, cookie, database, personal log or original private Git bundle with an issue. The local candidate is not approved for external publication until the final history/privacy/license gate and owner approval are complete.
