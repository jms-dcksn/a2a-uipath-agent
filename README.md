# a2a-uipath-agent

Skeleton [A2A](https://a2a-protocol.org/) remote agent server. It accepts a
message and replies `"Hello World"` — no LLM yet, just the protocol plumbing.

Built on **a2a-sdk 1.1.0** (HTTP transports only; no gRPC).

## Run

```bash
uv run server.py            # http://127.0.0.1:9999
```

## Try it

```bash
# Discovery — the agent's public "business card"
curl http://127.0.0.1:9999/.well-known/agent-card.json

# Send a message (note the required A2A-Version header)
curl -X POST http://127.0.0.1:9999/a2a/jsonrpc \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage",
       "params":{"message":{"messageId":"m1","role":"ROLE_USER",
                             "parts":[{"text":"hi there"}]}}}'
```

See [`WALKTHROUGH.md`](./WALKTHROUGH.md) for a line-by-line explanation of how it
works and where the AI logic plugs in later.
