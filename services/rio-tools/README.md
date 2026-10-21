# Registry and bounded SRT Builder

Existing service source, included as an optional interface implementation. The
architecture preview does not install a Registry, grant Docker access, create a
package or contact a model. Core uses private IPC for search/describe and calls
only an approved package with the expected hash.

`rio_tools.registry` stores packages and approval records. `executor` uses a fixed
Docker command/image contract; generated code is not imported by Core.
`builder` supports the existing SRT capability only, with at most two repairs.
`validator` checks fixed functional cases and isolation before activation.
Public IPC cannot approve a package or expand its permissions. These components
are not a general arbitrary-code sandbox.

Offline unit tests use mocks. Seven Docker integration probes are opt-in via
`RIO_TOOLS_TEST_IMAGE`; they remain present and skipped when that environment is
not supplied. They are outside the public preview's no-Docker acceptance claim.

```sh
python -m pip install -r services/rio-tools/requirements.txt pytest
python tools/ci/run_tests.py --suite tools
```

Use a separate virtual environment: Registry pins a different jsonschema version
from Core. Deployment/account scripts are reference examples, not portable
production installation instructions. No personal packages, operational DB,
image binary, credentials or historical device evidence are distributed.
See [security boundaries](../../docs/security-model.md).
