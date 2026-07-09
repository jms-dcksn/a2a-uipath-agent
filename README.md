# a2a-uipath-agent

A2A remote agent server backed by a `uipath-langchain` LangGraph wrapper. The
server runs outside UiPath, refreshes a UiPath external-app OAuth token, and
passes that bearer token to the configured UiPath MCP web-search server.

Built on **a2a-sdk 1.1.0** (HTTP transports only; no gRPC).

## Configure

Set these before sending requests:

```bash
export UIPATH_CLIENT_ID="<external-app-client-id>"
export UIPATH_CLIENT_SECRET="<external-app-client-secret>"
export A2A_BEARER_TOKEN="<token-clients-must-send>"
```

Optional overrides:

```bash
export UIPATH_URL="https://staging.uipath.com/uipathlabs/Playground"
export UIPATH_OAUTH_SCOPE="OR.Execution OR.Jobs"
export UIPATH_MCP_SERVER_URL="https://staging.uipath.com/uipathlabs/Playground/agenthub_/mcp/e072bd13-1c37-4125-a891-fde9bf3d7311/coded-web-search-server"
export UIPATH_AGENT_MODEL="gpt-4.1-mini-2025-04-14"
```

## Run

```bash
uv run server.py            # http://127.0.0.1:9999
```

## Try it

```bash
# Discovery - the agent's public "business card"
curl http://127.0.0.1:9999/.well-known/agent-card.json

# Send a message (note the required A2A-Version header)
curl -X POST http://127.0.0.1:9999/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -H "Authorization: Bearer $A2A_BEARER_TOKEN" \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage",
       "params":{"message":{"messageId":"m1","role":"ROLE_USER",
                             "parts":[{"text":"hi there"}]}}}'
```

See [`WALKTHROUGH.md`](./WALKTHROUGH.md) for a line-by-line explanation of how it
works.
