# Search runtime and planning contract repair

Search dependencies must be installed for the **adapter service's interpreter**,
not only in a developer/test environment. A successful Core-status or SRT probe
does not demonstrate that `web.search` can import DDGS or reach a provider.
The public hashed requirements already include DDGS and its runtime dependencies;
this change does not add a new search provider, model, server or credential.

For the supported preview environment, use the same interpreter to install and
probe dependencies, then test a bounded read-only search through the adapter:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-preview.lock
.venv/bin/python -c 'from ddgs import DDGS; DDGS(); print("search import ready")'
```

For separated service accounts, run the equivalent probe as the operator using
the adapter service's selected interpreter and dependency path. Do not install
packages globally or give the isolated Core new network/Docker permissions to
repair an operator-side dependency problem. An import probe is not a live search
test or an assurance that an external provider will stay available.

## Corrected behavior

- Search import, provider and empty-result failures return `UNAVAILABLE` with
  `SEARCH_DEPENDENCY_UNAVAILABLE`, `SEARCH_PROVIDER_UNAVAILABLE`, or
  `SEARCH_NO_RESULTS`. Raw provider exception strings are not exposed.
- Search snippets retain actual URLs and `content_read=false`. Attribution is
  not page-body retrieval, independent fact checking or authority to execute.
- `web.search` does not create `browser.search` result indexes. Index/back steps
  require the existing verified browser context, including after plan correction.
- Planner choices omit unavailable index/back tools and direct URL opening
  without an already authorized URL. Dispatch independently rechecks the same
  prerequisites. Asking to print a source URL does not authorize navigation.
- Duplicate-step correction remains one bounded model-only attempt. It does not
  dispatch the same ID again. If a URL/index plan is still invalid, the task stays
  `WAITING_INPUT` and retains its confirmed current-request search excerpts;
  the goal is not falsely marked complete.
- Response instructions require definitions/full names to match supplied
  evidence and distinguish evidence levels from physical/service states.
  Actual source URLs are retained when a model supplies only a source name.

The existing executor, task fencing, physical-command policy and no-blind-retry
rules are preserved. This is a contract repair, not a new planner or dictionary.
No term-specific answer or privileged action is built into the implementation.

## Verification and limits

Regression cases cover missing dependencies, provider-error redaction, no results,
successful source preservation, failed-ID lookup without another dispatch,
invalid browser-index correction, repeated invalid plans, valid search-to-open,
and source attribution. Existing safety checks remain in the release gate.

A controlled operating acceptance used a fresh search for a financial term's
English expansion, Korean name and source URL. It completed with `web.search`
as its only tool and returned the correct names and an actual source URL. Earlier
failed trials were retained, not relabeled as successful. This single acceptance
does not certify arbitrary model answers, current financial rates, uninterrupted
operation, physical devices or a real dedicated-browser connection.

The model can still propose invalid steps, search snippets may omit important
context, and external providers can fail. The bounded plan/dispatch checks and
the preserved evidence remain necessary. Source attribution alone does not prove
that every generated claim is supported.
