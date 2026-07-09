# A2A UiPath Agent Walkthrough

This server exposes a UiPath-backed LangChain agent through A2A. It keeps the
A2A transport small: incoming messages become A2A tasks, the executor calls the
agent, and the final answer is emitted as a response artifact.

## Request Flow

```
A2A request
  -> FastAPI routes
  -> DefaultRequestHandler
  -> UiPathAgentExecutor.execute()
  -> run_token_refresh_graph()
  -> UiPath MCP web-search server
  -> response artifact
```

`UiPathAgentExecutor` still owns the A2A task lifecycle:

1. Enqueue the initial `Task` in `TASK_STATE_SUBMITTED`.
2. Move the task to `TASK_STATE_WORKING`.
3. Call the UiPath LangChain agent with `context.get_user_input()`.
4. Emit the final answer as the `response` artifact.
5. Mark the task completed, or failed if the agent call raises.

## A2A Authentication

When `A2A_BEARER_TOKEN` is set, the FastAPI app requires
`Authorization: Bearer <token>` on `/a2a/jsonrpc` and `/a2a/rest`.
The agent card stays public at `/.well-known/agent-card.json` so clients can
discover the agent before authenticating task calls.

## Token Refresh

`ExternalAppTokenProvider` reads external-app configuration from environment
variables and lazily creates `uipath.platform.UiPath(...)` only when a request
needs a token. When external-app credentials are present, it fetches and caches
that token in process memory instead of reusing a user token from the environment.

`run_token_refresh_graph()` wraps the MCP call in a LangGraph state machine:

```
START
  -> connect_to_mcp        # if a cached token exists
  -> fetch_new_access_token # if no token exists
  -> connect_to_mcp
  -> END
```

If the MCP call returns a 401, the graph clears the cached token, fetches a fresh
one, and retries once. A second 401 is raised back to the A2A executor and the
task is marked failed.

## LangChain and MCP

`call_uipath_mcp_agent()` imports the LangChain, MCP, and UiPath LangChain
packages lazily. This keeps `server.py` import-safe when credentials are not set.

At runtime it:

1. Opens the MCP streamable HTTP client with
   `Authorization: Bearer <access_token>`.
2. Loads MCP tools with `langchain_mcp_adapters.tools.load_mcp_tools`.
3. Builds a `UiPathAzureChatOpenAI` model with the same access token.
4. Creates a LangChain agent with `create_agent`.
5. Invokes the agent with the system prompt and user task.

## Configuration

Required:

```bash
export UIPATH_CLIENT_ID="<external-app-client-id>"
export UIPATH_CLIENT_SECRET="<external-app-client-secret>"
export A2A_BEARER_TOKEN="<token-clients-must-send>"
```

Defaults target staging Playground and the supplied MCP endpoint. Override these
when needed:

```bash
export UIPATH_URL="https://staging.uipath.com/uipathlabs/Playground"
export UIPATH_OAUTH_SCOPE="OR.Execution OR.Jobs"
export UIPATH_MCP_SERVER_URL="https://staging.uipath.com/uipathlabs/Playground/agenthub_/mcp/e072bd13-1c37-4125-a891-fde9bf3d7311/coded-web-search-server"
export UIPATH_AGENT_MODEL="gpt-4.1-mini-2025-04-14"
```

If the MCP endpoint returns 403 after token refresh succeeds, verify both the
external app's granted scopes and its Orchestrator folder role assignment for the
folder that owns the MCP server.
