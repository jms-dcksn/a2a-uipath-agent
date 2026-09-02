# Plan: Tavily web search as a fallback MCP tool

## Why

The UiPath MCP web search server connects and lists its tools, but the tool
call itself fails. The agent then has nothing to fall back to and returns a
refusal. A second web search tool keeps the demo working while the UiPath
server is broken.

## Shape of the change

One LangChain agent, two MCP servers, tools merged into one tool list.

    invoke_uipath_agent
      -> run_token_refresh_graph      (unchanged: UiPath token + 401 retry)
        -> call_web_search_agent
             connect UiPath MCP   -> tools named uipath_*
             connect Tavily MCP   -> tools named tavily_*
             create_agent(model, tools = uipath_* + tavily_*)

`load_mcp_tools` already takes `server_name` and `tool_name_prefix`, so the
LangChain-facing names get a server prefix while each tool still calls its own
original MCP name. That keeps the two servers apart even if both expose a tool
called `search`, and it lets the system prompt name them.

## Steps

1. Add `McpServer` (name, url, headers, is_primary) and build the list from
   the environment. Tavily is included only when `TAVILY_API_KEY` is set.
2. Open both sessions under one `AsyncExitStack` and merge the tool lists.
3. Degrade instead of failing: if a server cannot be reached, log it and carry
   on with whatever tools loaded. Raise only when no server yielded any tool.
4. Keep the 401 retry working. A 401 from the primary UiPath server must still
   propagate, because `run_token_refresh_graph` watches for it to refresh the
   external-app token. A failure from a fallback server is always swallowed.
5. Rewrite the system prompt: search before answering, try `uipath_*` first,
   retry with `tavily_*` on error or empty result, name the tool that answered.
6. Log the tool names loaded per server, so the Fly log shows whether Tavily
   is actually wired.

## Configuration

| Name | Where | Purpose |
| --- | --- | --- |
| `TAVILY_API_KEY` | Fly secret | Enables the fallback. Absent means UiPath only. |
| `TAVILY_MCP_SERVER_URL` | fly.toml env, optional | Override the endpoint. Default `https://mcp.tavily.com/mcp/`. |
| `UIPATH_AGENT_SYSTEM_PROMPT` | fly.toml env, optional | Override the prompt. |

The key is passed to Tavily as the `tavilyApiKey` query parameter on the MCP
URL. If Tavily changes that contract, set `TAVILY_MCP_SERVER_URL` to the full
endpoint rather than changing code.

## What this does not do

The fallback is driven by the prompt, not by code inspecting tool results. The
model decides to retry with `tavily_*` after it sees the UiPath tool error.
That is enough for a demo and keeps one agent loop. A code-level fallback would
need the agent to run twice and a rule for what counts as failure.

It also does not fix the UiPath MCP tool call. That is still worth diagnosing
on the tenant side.

## Verification

- Unit tests for server-list building, graceful degradation, and the rule that
  a primary 401 still propagates.
- Fly log after a real run should show two `Loaded N tools from ...` lines.
