# Required MCP startup: actual-binary regression fixture

This generic fixture tests the headless Crustly process itself. It introduces
no runtime implementation and never replaces the client/provider path with a
mock or preflight. A local deterministic HTTP server counts model attempts;
real stdio subprocesses return MCP responses. No inference occurs.

Run on Linux with unprivileged user/network namespaces available:

```sh
python3 -I -B tests/required_mcp_startup.py \
  --binary /path/to/crustly \
  --artifacts /path/to/new-private-artifact-directory
```

The fixture creates a fresh network namespace with loopback only. It strips
credential environment variables, preserves HOME and CODEX_HOME unchanged,
and uses explicit disposable config/database/cache/working directories.
It does not need actual provider credentials, remote endpoints or installed
MCP registrations. It does not change host networking. Fixture child Tokio/Rayon worker counts
are bounded at two so concurrent host builds do not exhaust runtime workers.
The fixture prepares an empty SQLite WAL file; the actual client still opens
and migrates it. No application schema/session or native preflight is seeded.

| Case | Required contract |
|---|---|
| required initialization error | Nonzero client exit, exactly zero fake-model requests |
| required discovery error | Nonzero client exit, exactly zero fake-model requests |
| required good server plus required discovery error | Nonzero exit, zero requests; all children clean up |
| explicit optional initialization error | Existing optional behavior, successful fake completion |
| missing required flag plus discovery error | Existing default behavior, successful fake completion |
| required server with valid empty tools list | Successful discovery and fake completion |

The server accepts the stock client's missing initialized notification
deliberately, so that an unrelated interoperability failure cannot manufacture
a passing strict-startup assertion. Valid empty discovery is not a failure.
The implemented required=true field is a per-server, default-false option
for headless run startup. Required initialization or discovery RPC errors abort
before provider creation; missing or false preserves optional behavior.
Older binaries that ignore the field fail the required-server assertions while
compatibility controls pass. A timeout, configuration error, unreachable fixture,
or unreached MCP exchange is an invalid fixture result, not evidence of strict
startup.

Every HTTP method reaching the fake model is counted, including unexpected
paths. Counters are asserted after process exit, natural MCP child cleanup,
and fixture server shutdown. Failed clients are never killed early to create
a zero count. Timeouts have a separate failure category. Linux child subreaping
and bounded process-group cleanup prevent fixture leftovers.

results.json identifies the exact binary digest/version and fixture digest,
each observed exit/request/event count, environment-preservation checks and
cleanup outcome. Raw synthetic process output stays in private artifacts;
do not commit generated configs, databases, logs, receipts or binary files.
The test source and this document contain no installation-specific paths,
hostnames, tenant identities or secrets.

A binary hash plus a source reference does not establish build provenance.
Record the exact source commit, local patch diff and build toolchain/command
when building a later candidate; do not claim an existing patched binary was
built from a clean reference merely because both report the same version.

This is only the strict-startup prerequisite. It does not prove any particular
production launcher, user admission, authorization, gateway path, business
task output or deployment.
