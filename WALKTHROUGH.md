# A2A skeleton server — code walkthrough

This is a working [A2A](https://a2a-protocol.org/) remote agent server with no
intelligence yet. It accepts a message and always replies `"Hello World"`. The
point is to understand the *plumbing* — what an A2A server actually is — before
adding an LLM.

It uses **a2a-sdk 1.1.0**. Note: the SDK changed a lot between 0.x and 1.x. Most
"A2A hello world" tutorials online use the old `A2AStarletteApplication` API,
which no longer exists. This uses the 1.x routes-based API.

## What A2A is, in one paragraph

A2A (Agent-to-Agent) is a protocol for one agent to call another over HTTP. A
server agent publishes a machine-readable **Agent Card** describing who it is and
how to reach it. A client agent fetches that card, then sends it **messages**.
Each message kicks off a **task** with a lifecycle (submitted → working →
completed), and the agent streams back **status updates** and **artifacts** (the
actual output). That's it: a card for discovery, and a task state machine for
work.

## The five moving parts

```
HTTP request
    │
    ▼
FastAPI routes  ──►  DefaultRequestHandler  ──►  AgentExecutor.execute()
(transport)          (protocol ↔ your code)      (your logic — emits events)
                            │                              │
                            ▼                              ▼
                       TaskStore                      EventQueue
                  (remembers tasks)            (carries Task/status/artifact
                                                 events back to the client)
```

1. **AgentExecutor** — your logic. `execute()` is called per incoming message.
2. **EventQueue** — you push events onto it (the task, status changes, output).
3. **TaskStore** — server-side memory of tasks. We use the in-memory one.
4. **DefaultRequestHandler** — translates A2A protocol calls into executor calls.
5. **Routes / FastAPI** — the actual HTTP surface clients hit.

---

## Walking the code (`server.py`)

### 1. The executor — where the work happens

```python
class HelloWorldExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        ...
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        ...
```

`AgentExecutor` is an abstract base with two required async methods: `execute`
and `cancel`. This is the *only* class you'll meaningfully change as this grows
into a real agent.

- **`context: RequestContext`** — the incoming request. Useful bits:
  `context.message` (the raw message), `context.get_user_input()` (the text the
  user sent, flattened to a string), `context.task_id`, `context.context_id`.
  A `context_id` groups related tasks into a conversation; a `task_id` is one
  unit of work.
- **`event_queue: EventQueue`** — your *outbound* channel. You don't `return` a
  result; you `enqueue_event(...)` events, and the handler relays them to the
  client. This is what makes streaming possible.

Inside `execute`:

```python
await event_queue.enqueue_event(
    Task(id=task_id, context_id=context_id,
         status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
         history=[context.message] if context.message else []),
)
```

**The first event must be the `Task` object itself**, in the `SUBMITTED` state.
This was the one non-obvious gotcha: if you emit a status-update or artifact
before the protocol has "seen" the task created, you get
`-32006 Agent should enqueue Task before TaskStatusUpdateEvent event`. The
`Task` event is what registers the task with the client and the task store.

```python
updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
await updater.start_work()
```

`TaskUpdater` is a thin convenience wrapper over `event_queue` for *this* task.
Instead of hand-building status-update events, you call `start_work()`,
`complete()`, `add_artifact()`, `failed()`, `cancel()`, etc. `start_work()`
moves the task from `SUBMITTED` → `WORKING`.

```python
await updater.add_artifact(parts=[Part(text="Hello World")], name="response", last_chunk=True)
await updater.complete()
```

- **`add_artifact`** emits the actual output. An artifact has a `name` and a list
  of **`Part`**s. A `Part` is one piece of content — here just `text`, but a part
  can also carry files (`url`/`raw`), structured `data`, etc. `last_chunk=True`
  signals this artifact is fully delivered (relevant when you stream output in
  chunks).
- **`complete`** moves the task to `COMPLETED`, the terminal success state.

`cancel()` just marks the task cancelled — there's nothing long-running here to
interrupt.

> Note on enums: in 1.x, `TaskState` and `Role` are **protobuf** enums, not
> Python `str` enums. That's why you write `TaskState.TASK_STATE_SUBMITTED` and
> why a JSON message uses `"role": "ROLE_USER"`.

### 2. The Agent Card — discovery

```python
def build_agent_card(host, port) -> AgentCard:
    return AgentCard(
        name="Hello World Agent",
        description="...",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[AgentSkill(id="hello_world", name="Hello World", ...)],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", ...), ...],
    )
```

The card is pure metadata — it does no work. A client fetches it from
`/.well-known/agent-card.json` (a well-known path, like `robots.txt`) to decide
whether and how to talk to this agent. Key fields:

- **`capabilities`** — feature flags. `streaming=True` says the agent can stream
  events via Server-Sent Events.
- **`skills`** — advertised capabilities, for discovery/routing. They're
  descriptive labels, not enforced; our single skill maps loosely to what the
  executor does.
- **`supported_interfaces`** — the transports + URLs a client can use. We expose
  two HTTP transports off the same app:
  - **JSONRPC** at `/a2a/jsonrpc` — one POST endpoint, method in the body.
  - **HTTP+JSON** (REST) at `/a2a/rest` — RESTish paths like `/message:send`.

  The full SDK sample also exposes gRPC; we dropped it to keep the skeleton to
  one HTTP server and avoid the gRPC/protobuf toolchain.

### 3. Wiring it together

```python
request_handler = DefaultRequestHandler(
    agent_executor=HelloWorldExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=agent_card,
)
```

The `DefaultRequestHandler` is the hub: it receives parsed protocol requests,
invokes your executor, and pumps the executor's events back out. `InMemoryTaskStore`
keeps task state in a dict — fine for dev, lost on restart; swap for a SQL-backed
store later (the SDK ships `sql`/`postgresql`/`sqlite` extras).

```python
app = FastAPI()
add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(agent_card=agent_card),
    jsonrpc_routes=create_jsonrpc_routes(request_handler=request_handler, rpc_url="/a2a/jsonrpc"),
    rest_routes=create_rest_routes(request_handler=request_handler, path_prefix="/a2a/rest"),
)
```

Each `create_*_routes` builds the route group for one transport, all backed by
the same `request_handler`. `add_a2a_routes_to_fastapi` mounts them on a normal
FastAPI app — so you could add your own `/health` route, middleware, etc.
alongside. `uvicorn` then serves that app.

---

## Running and testing it

```bash
uv run server.py            # serves on http://127.0.0.1:9999
```

**1. Fetch the card** (discovery):

```bash
curl http://127.0.0.1:9999/.well-known/agent-card.json
```

**2. Send a message** (JSON-RPC). Two things trip people up:

- The method is **`SendMessage`** (1.x method names mirror the gRPC service —
  *not* `message/send` from older docs).
- You **must** send the `A2A-Version: 1.0` header. Without it the server assumes
  `0.3` and rejects the call with `VERSION_NOT_SUPPORTED`.

```bash
curl -X POST http://127.0.0.1:9999/a2a/jsonrpc \
  -H 'Content-Type: application/json' \
  -H 'A2A-Version: 1.0' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage",
       "params":{"message":{"messageId":"m1","role":"ROLE_USER",
                             "parts":[{"text":"hi there"}]}}}'
```

Response (trimmed) — a completed task carrying the `"Hello World"` artifact:

```json
{"result":{"task":{
  "status":{"state":"TASK_STATE_COMPLETED"},
  "artifacts":[{"name":"response","parts":[{"text":"Hello World"}]}],
  "history":[{"role":"ROLE_USER","parts":[{"text":"hi there"}]}]
}},"id":"1","jsonrpc":"2.0"}
```

**3. Same thing via REST** (same header rule applies):

```bash
curl -X POST http://127.0.0.1:9999/a2a/rest/message:send \
  -H 'Content-Type: application/json' -H 'A2A-Version: 1.0' \
  -d '{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}'
```

---

## Where the AI goes later

Everything outside `HelloWorldExecutor.execute()` stays roughly the same. To make
this a real agent, that method is where you'd:

1. Read the user input: `query = context.get_user_input()`.
2. Call an LLM / tool / chain with it.
3. Stream partial output by calling `updater.add_artifact(..., last_chunk=False)`
   repeatedly, then a final `last_chunk=True` — instead of one canned reply.
4. `updater.complete()` (or `updater.failed(...)` on error).

The protocol scaffolding — card, transports, task lifecycle — doesn't change.
That separation is the whole point of A2A: clients interact with the task state
machine, not with your model code.
```
