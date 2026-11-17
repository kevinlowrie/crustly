# Actual-binary native MCP compatibility regressions

Run this standalone Linux fixture with a real Crustly binary:

    python3 -I -B tests/mcp_native_protocol.py --binary /path/to/crustly --artifacts /path/to/new-private-directory

The existing frozen startup helper is imported unchanged for namespace setup
and child reaping. The actual binary uses real stdio subprocesses and a
deterministic loopback HTTP model server, with no inference or external route.
HOME/CODEX_HOME remain unchanged; credential env is removed and fixture paths
are explicit. No source implementation or provider is mocked.

| Case | Contract |
|---|---|
| strict initialized lifecycle | One initialized notification precedes tools/list; valid server reaches model |
| canonical inputSchema | Complete schema constraints reach the actual model request |
| malformed discovery | Required startup fails before any model request |
| lenient empty control | Valid tools:[] remains successful |
| legacy schema control | Existing input_schema compatibility preserves constraints |

The lenient/legacy controls deliberately isolate the known stock-client
compatibility paths. They do not redefine the standard MCP wire contract.
A timeout, unreached exchange, missing control tool or setup error is invalid
fixture evidence, not semantic RED. Counters are frozen after natural client
exit, child cleanup and server shutdown. Temporary data uses private tmpfs
when available; no storage durability is claimed.

No tool invocation or auto-approval occurs in this slice. MCP isError needs
its own actual tool-call evidence and reviewed synthetic approval method.

These native-client component tests do not prove the bridge, actual
caller/callee pairing, LiteLLM authorization, tenant membership or deployment.
A later fix or bridge adaptation must be separately assigned/reviewed.
