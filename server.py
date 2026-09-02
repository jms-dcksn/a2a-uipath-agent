"""A2A remote agent server backed by a UiPath LangChain agent.

Run it:
    uv run server.py
Then fetch its public "business card":
    curl http://127.0.0.1:9999/.well-known/agent-card.json
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any, TypedDict

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from request_logging import add_a2a_request_logging, is_request_logging_enabled

from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Part,
    SecurityRequirement,
    SecurityScheme,
    Task,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger(__name__)

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared for runtime.
    load_dotenv = None

if load_dotenv:
    load_dotenv()


DEFAULT_UIPATH_BASE_URL = "https://staging.uipath.com/uipathlabs/Playground"
DEFAULT_UIPATH_SCOPE = "OR.Execution OR.Jobs"
DEFAULT_MCP_SERVER_URL = (
    "https://staging.uipath.com/uipathlabs/Playground/agenthub_/mcp/"
    "e072bd13-1c37-4125-a891-fde9bf3d7311/coded-web-search-server"
)
DEFAULT_MODEL_NAME = "gpt-4.1-mini-2025-04-14"
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Use the available UiPath MCP tools when web "
    "search would improve the answer. Keep answers concise and cite sources "
    "returned by the tool when they are available."
)
A2A_PROTECTED_PATH_PREFIXES = ("/a2a/jsonrpc", "/a2a/rest", "/v1")
A2A_BEARER_SCHEME_NAME = "bearerAuth"

# The v0.3 HTTP+JSON routes that carry a message and therefore need the
# connector-compatibility shim below.
V0_3_MESSAGE_PATHS = ("/v1/message:send", "/v1/message:stream")
# A v0.3 Part is a oneof; exactly one of these keys carries the payload.
PART_CONTENT_KEYS = ("text", "file", "data")
# message:send returns as soon as the task is submitted, so the connector polls
# for the result. It polls the gRPC-transcoding custom-method spelling,
# GET /v1/tasks:get?id=<task id>. The SDK mounts only the resource spelling,
# GET /v1/tasks/{id}, so the poll hit no route at all and returned FastAPI's
# own 404 forever. Serve both spellings from the same handler.
V0_3_TASK_ALIASES = {
    ("/v1/tasks/{id}", "GET"): "/v1/tasks:get",
    ("/v1/tasks/{id}:cancel", "POST"): "/v1/tasks:cancel",
    ("/v1/tasks/{id}:subscribe", "GET"): "/v1/tasks:subscribe",
    ("/v1/tasks/{id}:subscribe", "POST"): "/v1/tasks:subscribe",
}
# Protobuf JSON wants the enum name. Accept the spellings other A2A bindings
# and older clients use.
ROLE_ALIASES = {
    "user": "ROLE_USER",
    "role_user": "ROLE_USER",
    "agent": "ROLE_AGENT",
    "assistant": "ROLE_AGENT",
    "role_agent": "ROLE_AGENT",
}

AgentCall = Callable[[str, str], Awaitable[str]]
_TOKEN_PROVIDER: "ExternalAppTokenProvider | None" = None


class TokenGraphState(TypedDict, total=False):
    task: str
    access_token: str | None
    refresh_attempted: bool
    result: str | None


class ExternalAppTokenProvider:
    """Refreshes and caches UiPath external-app access tokens."""

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str | None,
        client_secret: str | None,
        scope: str,
        environ: MutableMapping[str, str] | None = None,
        sdk_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.environ = environ if environ is not None else os.environ
        self._sdk_factory = sdk_factory
        self.cached_access_token = (
            None
            if self.client_id and self.client_secret
            else self.environ.get("UIPATH_ACCESS_TOKEN")
        )

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        if self.cached_access_token and not force_refresh:
            return self.cached_access_token

        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "Missing UiPath external app credentials. Set UIPATH_CLIENT_ID "
                "and UIPATH_CLIENT_SECRET before calling the agent."
            )

        sdk_factory = self._sdk_factory or _load_uipath_sdk_factory()
        sdk = sdk_factory(
            base_url=self.base_url,
            client_id=self.client_id,
            client_secret=self.client_secret,
            scope=self.scope,
        )
        token = (
            _extract_sdk_access_token(sdk)
            or getattr(sdk, "_access_token", None)
            or self.environ.get("UIPATH_ACCESS_TOKEN")
        )
        if not token:
            raise RuntimeError("UiPath SDK did not provide UIPATH_ACCESS_TOKEN.")

        self.cached_access_token = token
        return token

    def clear_cached_access_token(self) -> None:
        self.cached_access_token = None
        self.environ.pop("UIPATH_ACCESS_TOKEN", None)


def _load_uipath_sdk_factory() -> Callable[..., Any]:
    from uipath.platform import UiPath

    return UiPath


def _extract_sdk_access_token(sdk: Any) -> str | None:
    token = getattr(sdk, "access_token", None)
    if token:
        return token

    config = getattr(sdk, "_config", None)
    return getattr(config, "secret", None)


def build_token_provider_from_env() -> ExternalAppTokenProvider:
    return ExternalAppTokenProvider(
        base_url=os.getenv("UIPATH_BASE_URL")
        or os.getenv("UIPATH_URL")
        or DEFAULT_UIPATH_BASE_URL,
        client_id=os.getenv("UIPATH_CLIENT_ID")
        or os.getenv("UIPATH_EXTERNAL_APP_CLIENT_ID"),
        client_secret=os.getenv("UIPATH_CLIENT_SECRET")
        or os.getenv("UIPATH_EXTERNAL_APP_CLIENT_SECRET"),
        scope=os.getenv("UIPATH_OAUTH_SCOPE", DEFAULT_UIPATH_SCOPE),
    )


def get_token_provider() -> ExternalAppTokenProvider:
    global _TOKEN_PROVIDER
    if _TOKEN_PROVIDER is None:
        _TOKEN_PROVIDER = build_token_provider_from_env()
    return _TOKEN_PROVIDER


def add_a2a_bearer_auth(app: FastAPI, bearer_token: str | None) -> None:
    if not bearer_token:
        logger.warning("A2A_BEARER_TOKEN is not set; A2A routes are public.")
        return

    @app.middleware("http")
    async def require_a2a_bearer_token(request: Request, call_next):
        if not request.url.path.startswith(A2A_PROTECTED_PATH_PREFIXES):
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token,
            bearer_token,
        ):
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


def normalize_v0_3_part(part: Any) -> Any:
    """Turn a bare string into a text Part; leave real Parts alone."""
    return {"text": part} if isinstance(part, str) else part


def normalize_v0_3_message(message: Any) -> Any:
    """Rewrite a near-miss message into the fields the v0.3 binding parses.

    The UiPath Integration Service A2A connector sends its own activity fields
    rather than an A2A message: the skill id as "capabilities", and the message
    text as a list of bare strings under "content". That shape is invalid in
    both v0.3 and v1.0, so no amount of version support accepts it.
    """
    if not isinstance(message, dict):
        return message

    normalized = dict(message)

    # v0.3 JSON-RPC names the list "parts". The HTTP+JSON binding is generated
    # from the proto, where the same field is "content".
    if "content" not in normalized and isinstance(normalized.get("parts"), list):
        normalized["content"] = normalized.pop("parts")

    content = normalized.get("content")
    if isinstance(content, list):
        normalized["content"] = [normalize_v0_3_part(part) for part in content]

    # A skill id has no home on a v0.3 message. Keep it as metadata instead of
    # dropping it, so the executor can still see which skill was asked for.
    requested_skill = normalized.pop("capabilities", None)
    if requested_skill is not None:
        metadata = normalized.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata.setdefault("capabilities", requested_skill)
        normalized["metadata"] = metadata

    # An unset role decodes to ROLE_UNSPECIFIED, which the SDK maps to "agent".
    # The message would then look like the agent's own turn and
    # context.get_user_input() would return nothing.
    role = normalized.get("role")
    normalized["role"] = (
        ROLE_ALIASES.get(role.casefold(), role)
        if isinstance(role, str) and role
        else "ROLE_USER"
    )

    if not normalized.get("messageId"):
        normalized["messageId"] = uuid.uuid4().hex

    return normalized


def normalize_v0_3_message_payload(payload: Any) -> Any:
    """Normalize the message inside a v0.3 send/stream request body.

    Also ask for a blocking send. In v0.3 configuration.blocking defaults to
    false, so a caller that sends no configuration gets an immediate
    TASK_STATE_SUBMITTED and has to poll for the answer. The connector sends no
    configuration, polls once about a second later, and accepts that
    non-terminal task. Default to blocking so the answer rides back on the send
    response itself and no poll is needed.
    """
    if not isinstance(payload, dict) or "message" not in payload:
        return payload

    normalized = dict(payload)
    normalized["message"] = normalize_v0_3_message(payload["message"])

    configuration = normalized.get("configuration")
    configuration = dict(configuration) if isinstance(configuration, dict) else {}
    configuration.setdefault("blocking", True)
    normalized["configuration"] = configuration

    return normalized


def describe_invalid_v0_3_message(payload: Any) -> str | None:
    """Name the first field that stops this body becoming a v0.3 message."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"

    message = payload.get("message")
    if not isinstance(message, dict):
        return 'body must contain a "message" object'

    content = message.get("content")
    if not isinstance(content, list) or not content:
        return "message.content must be a non-empty list of parts"

    for index, part in enumerate(content):
        if not isinstance(part, dict) or not any(
            key in part for key in PART_CONTENT_KEYS
        ):
            return (
                f"message.content[{index}] must set one of "
                '"text", "file" or "data"'
            )

    return None


def invalid_argument_response(message: str) -> JSONResponse:
    """Match the shape the v0.3 adapter uses for its own error responses."""
    return JSONResponse(
        {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": message}},
        status_code=400,
    )


def request_with_json_body(request: Request, payload: Any) -> Request:
    """Return the same request with a replaced, already-buffered JSON body."""
    body = json.dumps(payload).encode()

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = dict(request.scope)
    scope["headers"] = [
        (name, value)
        for name, value in request.scope["headers"]
        if name != b"content-length"
    ] + [(b"content-length", str(len(body)).encode())]
    return Request(scope, receive)


def normalize_v0_3_message_body(
    endpoint: Callable[[Request], Awaitable[Any]],
) -> Callable[[Request], Awaitable[Any]]:
    """Normalize the body before the SDK parses it; reject what it cannot fix.

    The SDK parses with ignore_unknown_fields=True, so a wrong shape is dropped
    silently: an unrecognised part leaves an empty Part, FromProto.part raises
    ValueError, and the caller gets a 500 that names no field. Validate here so
    a bad body returns 400 with the offending field instead.
    """

    async def endpoint_with_normalized_body(request: Request) -> Any:
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body) if raw_body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return invalid_argument_response("body must be valid JSON")

        normalized = normalize_v0_3_message_payload(payload)
        error = describe_invalid_v0_3_message(normalized)
        if error:
            return invalid_argument_response(error)

        if normalized != payload:
            logger.info("Normalized a non-standard v0.3 message body.")
            request = request_with_json_body(request, normalized)

        return await endpoint(request)

    return endpoint_with_normalized_body


def read_task_id_from_query(
    endpoint: Callable[[Request], Awaitable[Any]],
) -> Callable[[Request], Awaitable[Any]]:
    """Feed a /v1/tasks:<verb>?id=... request to the /v1/tasks/{id} handler."""

    async def endpoint_with_task_id_path_param(request: Request) -> Any:
        task_id = request.query_params.get("id")
        if not task_id:
            return invalid_argument_response('query parameter "id" is required')

        scope = dict(request.scope)
        scope["path_params"] = {"id": task_id}
        return await endpoint(Request(scope, request.receive))

    return endpoint_with_task_id_path_param


def add_v0_3_routes(
    app: FastAPI,
    request_handler: DefaultRequestHandler,
) -> None:
    """Mount the complete HTTP+JSON route set supplied by the v0.3 adapter."""
    # Import after a2a.server.routes is initialized to avoid the SDK's circular
    # rest_adapter -> routes.__init__ -> rest_routes -> rest_adapter import.
    from a2a.compat.v0_3.rest_adapter import REST03Adapter

    adapter = REST03Adapter(http_handler=request_handler)
    for (path, method), endpoint in adapter.routes().items():
        if path in V0_3_MESSAGE_PATHS:
            endpoint = normalize_v0_3_message_body(endpoint)
        app.add_route(path, endpoint, methods=[method])

        alias_path = V0_3_TASK_ALIASES.get((path, method))
        if alias_path:
            app.add_route(
                alias_path,
                read_task_id_from_query(endpoint),
                methods=[method],
            )


def is_unauthorized_error(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 401
    response = getattr(error, "response", None)
    if getattr(error, "status_code", None) == 401:
        return True
    if getattr(response, "status_code", None) == 401:
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(is_unauthorized_error(item) for item in error.exceptions)
    return False


def build_token_refresh_graph(
    *,
    token_provider: Any,
    agent_call: AgentCall,
):
    async def fetch_new_access_token(state: TokenGraphState) -> Command:
        token = await token_provider.get_access_token(
            force_refresh=bool(state.get("refresh_attempted"))
            or not bool(state.get("access_token"))
        )
        return Command(update={"access_token": token})

    async def connect_to_mcp(state: TokenGraphState) -> Command:
        access_token = state.get("access_token")
        if not access_token:
            return Command(update={"access_token": None})

        try:
            result = await agent_call(state["task"], access_token)
        except BaseException as error:
            if is_unauthorized_error(error) and not state.get("refresh_attempted"):
                clear = getattr(token_provider, "clear_cached_access_token", None)
                if clear:
                    clear()
                return Command(update={"access_token": None, "refresh_attempted": True})
            raise

        return Command(update={"result": result})

    def route_start(state: TokenGraphState) -> str:
        return (
            "connect_to_mcp"
            if state.get("access_token")
            else "fetch_new_access_token"
        )

    def route_after_connect(state: TokenGraphState) -> str:
        return END if state.get("result") is not None else "fetch_new_access_token"

    builder = StateGraph(TokenGraphState)
    builder.add_node("fetch_new_access_token", fetch_new_access_token)
    builder.add_node("connect_to_mcp", connect_to_mcp)
    builder.add_conditional_edges(START, route_start)
    builder.add_edge("fetch_new_access_token", "connect_to_mcp")
    builder.add_conditional_edges("connect_to_mcp", route_after_connect)
    return builder.compile()


async def run_token_refresh_graph(
    *,
    task: str,
    token_provider: Any,
    agent_call: AgentCall,
) -> str:
    graph = build_token_refresh_graph(
        token_provider=token_provider,
        agent_call=agent_call,
    )
    result = await graph.ainvoke(
        {
            "task": task,
            "access_token": getattr(token_provider, "cached_access_token", None),
            "refresh_attempted": False,
            "result": None,
        }
    )
    if not result.get("result"):
        raise RuntimeError("UiPath agent completed without a result.")
    return result["result"]


async def call_uipath_mcp_agent(
    task: str,
    access_token: str,
    *,
    mcp_server_url: str | None = None,
    model_name: str | None = None,
    system_prompt: str | None = None,
) -> str:
    from langchain.agents import create_agent
    from langchain_mcp_adapters.tools import load_mcp_tools
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        from langchain.messages import HumanMessage, SystemMessage
    except ImportError:
        from langchain_core.messages import HumanMessage, SystemMessage

    resolved_mcp_url = mcp_server_url or os.getenv(
        "UIPATH_MCP_SERVER_URL", DEFAULT_MCP_SERVER_URL
    )
    resolved_model_name = model_name or os.getenv(
        "UIPATH_AGENT_MODEL", DEFAULT_MODEL_NAME
    )
    resolved_system_prompt = system_prompt or os.getenv(
        "UIPATH_AGENT_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT
    )

    async with streamablehttp_client(
        url=resolved_mcp_url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=60,
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await load_mcp_tools(session)
            model = build_chat_model(
                model_name=resolved_model_name,
                access_token=access_token,
            )
            agent = create_agent(model, tools=tools)
            response = await agent.ainvoke(
                {
                    "messages": [
                        SystemMessage(content=resolved_system_prompt),
                        HumanMessage(content=task),
                    ]
                }
            )
            return _last_message_content(response)


def _last_message_content(response: Any) -> str:
    messages = response.get("messages") if isinstance(response, dict) else None
    if not messages:
        raise RuntimeError("LangChain agent response did not include messages.")

    content = getattr(messages[-1], "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", str(item)) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def build_chat_model(
    *,
    model_name: str,
    access_token: str | None = None,
    openai_factory: Callable[..., Any] | None = None,
) -> Any:
    if openai_factory is None:
        from uipath_langchain.chat.models import UiPathAzureChatOpenAI

        openai_factory = UiPathAzureChatOpenAI
    kwargs = {"model": model_name}
    if access_token:
        kwargs["access_token"] = access_token
    return openai_factory(**kwargs)


async def invoke_uipath_agent(user_input: str) -> str:
    token_provider = get_token_provider()
    return await run_token_refresh_graph(
        task=user_input,
        token_provider=token_provider,
        agent_call=call_uipath_mcp_agent,
    )


class UiPathAgentExecutor(AgentExecutor):
    """A2A executor that delegates each message to the UiPath LangChain agent."""

    def __init__(
        self,
        agent_runner: Callable[[str], Awaitable[str]] = invoke_uipath_agent,
    ):
        self.agent_runner = agent_runner

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        task_id = context.task_id or ""
        context_id = context.context_id or ""

        # First event must be the Task itself, in the SUBMITTED state. The
        # protocol rejects status/artifact events for a task it hasn't seen yet.
        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message] if context.message else [],
            )
        )

        # A TaskUpdater is the handle for emitting lifecycle events for this task.
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=task_id,
            context_id=context_id,
        )

        # Move the task into the "working" state. A single call can take about
        # 30 seconds, so give streaming clients something to show meanwhile.
        await updater.start_work(
            message=updater.new_agent_message(
                parts=[Part(text="Connecting to the UiPath MCP web search tool.")]
            )
        )

        user_input = context.get_user_input()
        logger.info("Received: %r", user_input)

        try:
            result = await self.agent_runner(user_input)
        except Exception as error:
            logger.exception("UiPath agent execution failed")
            await updater.failed(
                message=updater.new_agent_message(
                    parts=[
                        Part(
                            text=(
                                "UiPath agent failed while processing the request: "
                                f"{error}"
                            )
                        )
                    ]
                )
            )
            return

        await updater.add_artifact(
            parts=[Part(text=result)], name="response", last_chunk=True
        )
        # Carry the answer on the final status too. A polling client reads the
        # terminal task, and not every client looks inside task.artifacts.
        await updater.complete(
            message=updater.new_agent_message(parts=[Part(text=result)])
        )

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        # Nothing long-running to cancel in this skeleton.
        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id or "",
            context_id=context.context_id or "",
        )
        await updater.cancel()


def build_agent_card(host: str, port: int) -> AgentCard:
    """The Agent Card is the public, discoverable description of this agent.

    A2A clients fetch it from /.well-known/agent-card.json to learn the agent's
    name, what it can do (skills), and how to reach it (interfaces).
    """
    base_url = os.getenv("A2A_PUBLIC_URL", f"http://{host}:{port}").rstrip("/")

    # Clients only send a token if the card asks for one. Advertise the bearer
    # scheme whenever the middleware is armed, so the two never disagree.
    security_schemes = {}
    security_requirements = []
    if os.getenv("A2A_BEARER_TOKEN"):
        security_schemes[A2A_BEARER_SCHEME_NAME] = SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(
                scheme="bearer",
                description="Shared bearer token issued by the agent owner.",
            )
        )
        security_requirements.append(
            SecurityRequirement(schemes={A2A_BEARER_SCHEME_NAME: {}})
        )

    return AgentCard(
        name="UiPath Web Search Agent",
        description=(
            "A2A agent backed by a UiPath LangChain agent and an authenticated "
            "UiPath MCP web-search tool."
        ),
        provider=AgentProvider(
            organization="UiPath Playground",
            url="https://staging.uipath.com/uipathlabs/Playground",
        ),
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        security_schemes=security_schemes,
        security_requirements=security_requirements,
        skills=[
            AgentSkill(
                id="uipath_web_search",
                name="UiPath MCP Web Search",
                description=(
                    "Answers questions using the configured UiPath MCP web search server."
                ),
                tags=["uipath", "mcp", "web-search"],
                examples=["Search the web for the latest UiPath AgentHub updates."],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
        # How clients can actually talk to this agent. We expose two HTTP
        # transports off the same FastAPI app; no gRPC in this skeleton.
        #
        # The same JSON-RPC endpoint is declared twice, once per protocol
        # version. A client that speaks v0.3 - the UiPath Orchestrator Agent
        # Gateway does - looks for a v0.3 interface and cannot use a card that
        # only offers 1.0. The endpoint itself serves both, because the routes
        # are built with enable_v0_3_compat=True.
        #
        # Order matters for v0.3 clients. The SDK builds the legacy card from
        # the FIRST interface whose version is 0.3 or empty, and that one
        # supplies the legacy "url" and "preferredTransport". The UiPath
        # Integration Service A2A connector needs HTTP+JSON and appends
        # /v1/message:send to the url itself, so the HTTP+JSON 0.3 entry comes
        # first and carries the bare base URL with no path.
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{base_url}/a2a/jsonrpc",
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version="0.3",
                url=base_url,
            ),
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="0.3",
                url=f"{base_url}/a2a/jsonrpc",
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
                url=f"{base_url}/a2a/rest",
            ),
        ],
    )


def build_legacy_agent_card(agent_card: AgentCard) -> dict[str, Any]:
    """Return the same agent in the pre-1.0 card shape.

    Cards before v1.0 carry a single "url" plus "preferredTransport" instead of
    "supportedInterfaces". A client written against that spec cannot resolve an
    endpoint from a 1.0 card, so serve it a shape it understands.
    """
    jsonrpc_url = next(
        (
            interface.url
            for interface in agent_card.supported_interfaces
            if interface.protocol_binding == "JSONRPC"
        ),
        "",
    )
    card: dict[str, Any] = {
        "name": agent_card.name,
        "description": agent_card.description,
        "url": jsonrpc_url,
        "preferredTransport": "JSONRPC",
        "protocolVersion": "0.3.0",
        "version": agent_card.version,
        "provider": {
            "organization": agent_card.provider.organization,
            "url": agent_card.provider.url,
        },
        "capabilities": {
            "streaming": agent_card.capabilities.streaming,
            "pushNotifications": agent_card.capabilities.push_notifications,
        },
        "defaultInputModes": list(agent_card.default_input_modes),
        "defaultOutputModes": list(agent_card.default_output_modes),
        "skills": [
            {
                "id": skill.id,
                "name": skill.name,
                "description": skill.description,
                "tags": list(skill.tags),
                "examples": list(skill.examples),
                "inputModes": list(skill.input_modes),
                "outputModes": list(skill.output_modes),
            }
            for skill in agent_card.skills
        ],
    }
    if A2A_BEARER_SCHEME_NAME in agent_card.security_schemes:
        card["securitySchemes"] = {
            A2A_BEARER_SCHEME_NAME: {"type": "http", "scheme": "bearer"}
        }
        card["security"] = [{A2A_BEARER_SCHEME_NAME: []}]
    return card


def add_legacy_agent_card_route(app: FastAPI, agent_card: AgentCard) -> None:
    """Serve the pre-1.0 well-known path, which the SDK no longer mounts."""
    legacy_card = build_legacy_agent_card(agent_card)

    @app.get("/.well-known/agent.json")
    async def legacy_agent_card() -> JSONResponse:
        return JSONResponse(legacy_card)


def build_app(host: str, port: int) -> FastAPI:
    """Wire the executor + agent card into a FastAPI app exposing A2A routes."""
    agent_card = build_agent_card(host, port)

    # The request handler is the bridge between the A2A protocol (incoming
    # requests) and your executor (the work). The task store tracks task state.
    request_handler = DefaultRequestHandler(
        agent_executor=UiPathAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    app = FastAPI()
    add_a2a_request_logging(
        app,
        enabled=is_request_logging_enabled(os.getenv("A2A_REQUEST_LOGGING")),
    )
    # Register auth after request logging so Starlette makes auth the outer
    # middleware and rejects unauthorized requests before reading their bodies.
    add_a2a_bearer_auth(app, os.getenv("A2A_BEARER_TOKEN"))
    # Mount legacy routes before the SDK's JSON-RPC tenant catch-all.
    add_legacy_agent_card_route(app, agent_card)
    add_v0_3_routes(app, request_handler)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=agent_card),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler=request_handler,
            rpc_url="/a2a/jsonrpc",
            # Without this, a client that omits the A2A-Version header is read
            # as v0.3 and rejected, and v0.3 method names such as
            # "message/send" return -32601.
            enable_v0_3_compat=True,
        ),
        rest_routes=create_rest_routes(
            request_handler=request_handler, path_prefix="/a2a/rest"
        ),
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="UiPath-backed A2A agent server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()

    app = build_app(args.host, args.port)

    logger.info(
        "Agent card: http://%s:%s/.well-known/agent-card.json", args.host, args.port
    )
    config = uvicorn.Config(app, host=args.host, port=args.port)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
