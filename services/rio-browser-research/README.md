# Dedicated research browser interface

Existing private IPC restricts operations to status, bounded search results,
selection/opening of a result, and stored navigation context. It does not expose
arbitrary scripts, cookies, login forms, device controls or browser permissions.
The underlying Playwright MCP implementation is an external dependency, not
bundled or installed by the offline preview.

Extension mode requires human approval of the dedicated Edge tab. Managed and
receipt-verified local CDP transports use separate profiles and connection
records. No personal profile, cookie or authentication state is copied. The
public preview does not start a browser or authorize a debugging connection.

Search/open results distinguish title/URL/snippet from content. A successful
navigation must not be reported as reading the page body or fact checking it.
Result indexes, site restrictions, navigation context and command variants have
automated coverage. Prior deployment logs/screenshots are private and excluded;
this preview does not claim a new live browser validation.

```sh
python tools/ci/run_tests.py --suite browser
```

Use the root preview dependency environment. Application URL restrictions are
not an OS sandbox or a browser-wide network boundary. See the
[security model](../../docs/security-model.md).
