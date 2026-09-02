import unittest
from unittest.mock import patch

import httpx
from a2a.types import Message, Part, Role, Task, TaskState
from fastapi.testclient import TestClient

from server import (
    DEFAULT_MODEL_NAME,
    DEFAULT_TAVILY_MCP_SERVER_URL,
    ExternalAppTokenProvider,
    UiPathAgentExecutor,
    build_app,
    build_mcp_servers,
    build_tavily_mcp_server,
    build_chat_model,
    build_agent_card,
    build_token_provider_from_env,
    describe_invalid_v0_3_message,
    is_unauthorized_error,
    normalize_v0_3_message_payload,
    run_token_refresh_graph,
)


class ExternalAppTokenProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_access_token_initializes_sdk_once_and_caches_token(self):
        env = {}
        calls = []

        def fake_sdk_factory(**kwargs):
            calls.append(kwargs)
            env["UIPATH_ACCESS_TOKEN"] = "token-1"
            return object()

        provider = ExternalAppTokenProvider(
            base_url="https://staging.uipath.com/uipathlabs/Playground",
            client_id="client-id",
            client_secret="client-secret",
            scope="OR.Execution OR.Jobs",
            environ=env,
            sdk_factory=fake_sdk_factory,
        )

        first = await provider.get_access_token()
        second = await provider.get_access_token()

        self.assertEqual(first, "token-1")
        self.assertEqual(second, "token-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            {
                "base_url": "https://staging.uipath.com/uipathlabs/Playground",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scope": "OR.Execution OR.Jobs",
            },
        )

    async def test_get_access_token_prefers_external_app_over_env_user_token(self):
        class FakeSdk:
            class Config:
                secret = "external-app-token"

            _config = Config()

        env = {"UIPATH_ACCESS_TOKEN": "user-token"}
        provider = ExternalAppTokenProvider(
            base_url="https://staging.uipath.com/uipathlabs/Playground",
            client_id="client-id",
            client_secret="client-secret",
            scope="OR.Execution OR.Jobs",
            environ=env,
            sdk_factory=lambda **_kwargs: FakeSdk(),
        )

        token = await provider.get_access_token()

        self.assertEqual(token, "external-app-token")
        self.assertEqual(provider.cached_access_token, "external-app-token")

    async def test_get_access_token_force_refresh_requests_new_sdk_token(self):
        env = {}
        calls = []

        def fake_sdk_factory(**kwargs):
            calls.append(kwargs)
            env["UIPATH_ACCESS_TOKEN"] = f"token-{len(calls)}"
            return object()

        provider = ExternalAppTokenProvider(
            base_url="https://staging.uipath.com/uipathlabs/Playground",
            client_id="client-id",
            client_secret="client-secret",
            scope="OR.Execution OR.Jobs",
            environ=env,
            sdk_factory=fake_sdk_factory,
        )

        await provider.get_access_token()
        refreshed = await provider.get_access_token(force_refresh=True)

        self.assertEqual(refreshed, "token-2")
        self.assertEqual(len(calls), 2)

    async def test_get_access_token_reads_sdk_config_secret(self):
        class FakeSdk:
            class Config:
                secret = "sdk-token"

            _config = Config()

        env = {}
        provider = ExternalAppTokenProvider(
            base_url="https://staging.uipath.com/uipathlabs/Playground",
            client_id="client-id",
            client_secret="client-secret",
            scope="OR.Execution OR.Jobs",
            environ=env,
            sdk_factory=lambda **_kwargs: FakeSdk(),
        )

        token = await provider.get_access_token()

        self.assertEqual(token, "sdk-token")
        self.assertEqual(provider.cached_access_token, "sdk-token")

    async def test_get_access_token_force_refresh_prefers_sdk_over_stale_env_token(self):
        class FakeSdk:
            class Config:
                secret = "fresh-token"

            _config = Config()

        env = {"UIPATH_ACCESS_TOKEN": "stale-token"}
        provider = ExternalAppTokenProvider(
            base_url="https://staging.uipath.com/uipathlabs/Playground",
            client_id="client-id",
            client_secret="client-secret",
            scope="OR.Execution OR.Jobs",
            environ=env,
            sdk_factory=lambda **_kwargs: FakeSdk(),
        )

        token = await provider.get_access_token(force_refresh=True)

        self.assertEqual(token, "fresh-token")

    def test_clear_cached_access_token_removes_env_token(self):
        env = {"UIPATH_ACCESS_TOKEN": "expired-token"}
        provider = ExternalAppTokenProvider(
            base_url="https://staging.uipath.com/uipathlabs/Playground",
            client_id="client-id",
            client_secret="client-secret",
            scope="OR.Execution OR.Jobs",
            environ=env,
            sdk_factory=lambda **_kwargs: object(),
        )

        provider.clear_cached_access_token()

        self.assertIsNone(provider.cached_access_token)
        self.assertNotIn("UIPATH_ACCESS_TOKEN", env)

    def test_build_token_provider_from_env_accepts_uipath_url_alias(self):
        with patch.dict(
            "os.environ",
            {
                "UIPATH_URL": "https://staging.uipath.com/uipathlabs/Playground",
                "UIPATH_CLIENT_ID": "client-id",
                "UIPATH_CLIENT_SECRET": "client-secret",
                "UIPATH_OAUTH_SCOPE": "OR.Execution OR.Jobs",
            },
            clear=True,
        ):
            provider = build_token_provider_from_env()

        self.assertEqual(
            provider.base_url,
            "https://staging.uipath.com/uipathlabs/Playground",
        )
        self.assertEqual(provider.client_id, "client-id")
        self.assertEqual(provider.client_secret, "client-secret")
        self.assertEqual(provider.scope, "OR.Execution OR.Jobs")


class TokenRefreshGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_token_refresh_graph_retries_once_after_mcp_401(self):
        class FakeTokenProvider:
            cached_access_token = "expired-token"

            def __init__(self):
                self.force_refresh_values = []
                self.clear_count = 0

            async def get_access_token(self, force_refresh=False):
                self.force_refresh_values.append(force_refresh)
                self.cached_access_token = "fresh-token"
                return self.cached_access_token

            def clear_cached_access_token(self):
                self.clear_count += 1
                self.cached_access_token = None

        seen_tokens = []

        async def fake_agent_call(task, access_token):
            seen_tokens.append((task, access_token))
            if access_token == "expired-token":
                request = httpx.Request("POST", "https://example.com/mcp")
                response = httpx.Response(401, request=request)
                raise httpx.HTTPStatusError(
                    "unauthorized",
                    request=request,
                    response=response,
                )
            return "Search answer"

        provider = FakeTokenProvider()

        result = await run_token_refresh_graph(
            task="latest UiPath agent news",
            token_provider=provider,
            agent_call=fake_agent_call,
        )

        self.assertEqual(result, "Search answer")
        self.assertEqual(
            seen_tokens,
            [
                ("latest UiPath agent news", "expired-token"),
                ("latest UiPath agent news", "fresh-token"),
            ],
        )
        self.assertEqual(provider.clear_count, 1)
        self.assertEqual(provider.force_refresh_values, [True])

    async def test_run_token_refresh_graph_retries_once_after_llm_401(self):
        class AuthenticationError(Exception):
            status_code = 401

        class FakeTokenProvider:
            cached_access_token = "expired-token"

            def __init__(self):
                self.force_refresh_values = []
                self.clear_count = 0

            async def get_access_token(self, force_refresh=False):
                self.force_refresh_values.append(force_refresh)
                self.cached_access_token = "fresh-token"
                return self.cached_access_token

            def clear_cached_access_token(self):
                self.clear_count += 1
                self.cached_access_token = None

        seen_tokens = []

        async def fake_agent_call(_task, access_token):
            seen_tokens.append(access_token)
            if access_token == "expired-token":
                raise AuthenticationError("expired LLM token")
            return "Search answer"

        provider = FakeTokenProvider()

        result = await run_token_refresh_graph(
            task="latest UiPath agent news",
            token_provider=provider,
            agent_call=fake_agent_call,
        )

        self.assertEqual(result, "Search answer")
        self.assertEqual(seen_tokens, ["expired-token", "fresh-token"])
        self.assertEqual(provider.clear_count, 1)
        self.assertEqual(provider.force_refresh_values, [True])

    def test_is_unauthorized_error_recognizes_response_status(self):
        class ResponseBackedError(Exception):
            def __init__(self):
                self.response = httpx.Response(401)

        self.assertTrue(is_unauthorized_error(ResponseBackedError()))


class AgentModelTests(unittest.TestCase):
    def test_default_model_is_openai(self):
        self.assertEqual(DEFAULT_MODEL_NAME, "gpt-4.1-mini-2025-04-14")

    def test_build_chat_model_uses_openai_passthrough_factory(self):
        calls = []

        def fake_openai_factory(**kwargs):
            calls.append(kwargs)
            return {"model": kwargs["model"]}

        model = build_chat_model(
            model_name="gpt-4o-2024-11-20",
            openai_factory=fake_openai_factory,
        )

        self.assertEqual(model, {"model": "gpt-4o-2024-11-20"})
        self.assertEqual(calls, [{"model": "gpt-4o-2024-11-20"}])

    def test_build_chat_model_passes_access_token_to_openai_factory(self):
        calls = []

        def fake_openai_factory(**kwargs):
            calls.append(kwargs)
            return {"model": kwargs["model"], "access_token": kwargs["access_token"]}

        model = build_chat_model(
            model_name="gpt-4.1-mini-2025-04-14",
            access_token="external-app-token",
            openai_factory=fake_openai_factory,
        )

        self.assertEqual(
            model,
            {
                "model": "gpt-4.1-mini-2025-04-14",
                "access_token": "external-app-token",
            },
        )
        self.assertEqual(
            calls,
            [
                {
                    "model": "gpt-4.1-mini-2025-04-14",
                    "access_token": "external-app-token",
                }
            ],
        )


class A2ABearerAuthTests(unittest.TestCase):
    def test_agent_card_stays_public_when_bearer_token_is_configured(self):
        with patch.dict("os.environ", {"A2A_BEARER_TOKEN": "secret-token"}):
            client = TestClient(build_app("127.0.0.1", 9999))

        response = client.get("/.well-known/agent-card.json")

        self.assertEqual(response.status_code, 200)

    def test_a2a_routes_require_bearer_token_when_configured(self):
        with patch.dict("os.environ", {"A2A_BEARER_TOKEN": "secret-token"}):
            client = TestClient(build_app("127.0.0.1", 9999))

        response = client.post(
            "/a2a/jsonrpc",
            headers={"A2A-Version": "1.0"},
            json={},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_a2a_routes_accept_valid_bearer_token_when_configured(self):
        with patch.dict("os.environ", {"A2A_BEARER_TOKEN": "secret-token"}):
            client = TestClient(build_app("127.0.0.1", 9999))

        response = client.get(
            "/a2a/jsonrpc",
            headers={
                "A2A-Version": "1.0",
                "Authorization": "Bearer secret-token",
            },
        )

        self.assertNotEqual(response.status_code, 401)


class UiPathAgentExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_execute_emits_agent_result_as_response_artifact(self):
        calls = []

        async def fake_agent_runner(user_input):
            calls.append(user_input)
            return "Found three relevant results."

        executor = UiPathAgentExecutor(agent_runner=fake_agent_runner)
        event_queue = FakeEventQueue()
        context = FakeRequestContext(user_input="search for UiPath MCP docs")

        await executor.execute(context, event_queue)

        self.assertEqual(calls, ["search for UiPath MCP docs"])
        self.assertIsInstance(event_queue.events[0], Task)
        self.assertEqual(
            event_queue.events[0].status.state,
            TaskState.TASK_STATE_SUBMITTED,
        )
        artifacts = [
            event
            for event in event_queue.events
            if type(event).__name__ == "TaskArtifactUpdateEvent"
        ]
        self.assertEqual(
            artifacts[0].artifact.parts[0].text,
            "Found three relevant results.",
        )
        self.assertTrue(artifacts[0].last_chunk)
        states = [
            event.status.state
            for event in event_queue.events
            if type(event).__name__ == "TaskStatusUpdateEvent"
        ]
        self.assertEqual(states[-1], TaskState.TASK_STATE_COMPLETED)

    async def test_execute_marks_task_failed_when_agent_raises(self):
        async def failing_agent_runner(_user_input):
            raise RuntimeError("MCP connection failed")

        executor = UiPathAgentExecutor(agent_runner=failing_agent_runner)
        event_queue = FakeEventQueue()
        context = FakeRequestContext(user_input="search")

        with self.assertLogs("server", level="ERROR"):
            await executor.execute(context, event_queue)

        statuses = [
            event
            for event in event_queue.events
            if type(event).__name__ == "TaskStatusUpdateEvent"
        ]
        self.assertEqual(statuses[-1].status.state, TaskState.TASK_STATE_FAILED)
        self.assertIn(
            "MCP connection failed",
            statuses[-1].status.message.parts[0].text,
        )


class AgentCardTests(unittest.TestCase):
    def test_build_agent_card_describes_uipath_web_search_agent(self):
        with patch.dict("os.environ", {}, clear=True):
            card = build_agent_card("127.0.0.1", 9999)

        self.assertEqual(card.name, "UiPath Web Search Agent")
        self.assertIn("UiPath MCP", card.description)
        self.assertEqual(card.provider.organization, "UiPath Playground")
        self.assertEqual(
            card.provider.url,
            "https://staging.uipath.com/uipathlabs/Playground",
        )
        self.assertEqual(card.skills[0].id, "uipath_web_search")
        self.assertEqual(card.skills[0].tags, ["uipath", "mcp", "web-search"])
        self.assertEqual(
            card.supported_interfaces[0].url,
            "http://127.0.0.1:9999/a2a/jsonrpc",
        )

    def test_build_agent_card_uses_public_url_override(self):
        with patch.dict(
            "os.environ",
            {"A2A_PUBLIC_URL": "https://tidy-log-6164.fly.dev/"},
        ):
            card = build_agent_card("0.0.0.0", 8080)

        interfaces = [
            (item.protocol_binding, item.protocol_version, item.url)
            for item in card.supported_interfaces
        ]
        self.assertEqual(
            interfaces,
            [
                ("JSONRPC", "1.0", "https://tidy-log-6164.fly.dev/a2a/jsonrpc"),
                # The first v0.3 entry becomes the legacy card's url and
                # preferredTransport. The UiPath Integration Service A2A
                # connector needs HTTP+JSON on the bare base URL, because it
                # appends /v1/message:send itself.
                ("HTTP+JSON", "0.3", "https://tidy-log-6164.fly.dev"),
                ("JSONRPC", "0.3", "https://tidy-log-6164.fly.dev/a2a/jsonrpc"),
                ("HTTP+JSON", "1.0", "https://tidy-log-6164.fly.dev/a2a/rest"),
            ],
        )


class FakeEventQueue:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class FakeRequestContext:
    task_id = "task-1"
    context_id = "context-1"

    def __init__(self, user_input):
        self._user_input = user_input
        self.message = Message(
            role=Role.ROLE_USER,
            message_id="message-1",
            parts=[Part(text=user_input)],
        )

    def get_user_input(self):
        return self._user_input


CONNECTOR_MESSAGE_BODY = {
    "message": {
        "capabilities": "uipath_web_search",
        "content": ["latest US Open news"],
    }
}


class McpServerSelectionTests(unittest.TestCase):
    def test_tavily_is_absent_without_an_api_key(self):
        servers = build_mcp_servers("token-1", environ={})

        self.assertEqual([server.name for server in servers], ["uipath"])
        self.assertTrue(servers[0].is_primary)
        self.assertEqual(
            servers[0].headers, {"Authorization": "Bearer token-1"}
        )

    def test_tavily_is_added_when_an_api_key_is_set(self):
        servers = build_mcp_servers(
            "token-1", environ={"TAVILY_API_KEY": "tvly-key"}
        )

        self.assertEqual([server.name for server in servers], ["uipath", "tavily"])
        self.assertFalse(servers[1].is_primary)

    def test_tavily_url_carries_the_api_key(self):
        server = build_tavily_mcp_server({"TAVILY_API_KEY": "tvly-key"})

        self.assertEqual(
            server.url, f"{DEFAULT_TAVILY_MCP_SERVER_URL}?tavilyApiKey=tvly-key"
        )

    def test_a_tavily_url_override_with_a_query_string_is_left_alone(self):
        server = build_tavily_mcp_server(
            {
                "TAVILY_API_KEY": "tvly-key",
                "TAVILY_MCP_SERVER_URL": "https://example.test/mcp?token=abc",
            }
        )

        self.assertEqual(server.url, "https://example.test/mcp?token=abc")


class McpToolLoadingTests(unittest.IsolatedAsyncioTestCase):
    """Degrade to the servers that answer, but let a primary 401 escape."""

    class FakeTool:
        def __init__(self, name):
            self.name = name

    async def _load(self, results):
        """results maps a server name to its tool names, or to an exception."""
        import contextlib
        import sys
        import types
        from unittest.mock import patch

        import server

        @contextlib.asynccontextmanager
        async def fake_client(*, url, headers, timeout):
            yield (url, None, None)

        class FakeSession:
            def __init__(self, url):
                self.url = url

            async def initialize(self):
                pass

        @contextlib.asynccontextmanager
        async def fake_client_session(read, write):
            yield FakeSession(read)

        async def fake_load_mcp_tools(session, *, server_name, tool_name_prefix):
            outcome = results[server_name]
            if isinstance(outcome, Exception):
                raise outcome
            return [McpToolLoadingTests.FakeTool(f"{server_name}_{n}") for n in outcome]

        mcp_module = types.ModuleType("mcp")
        mcp_module.ClientSession = fake_client_session
        http_module = types.ModuleType("mcp.client.streamable_http")
        http_module.streamablehttp_client = fake_client
        tools_module = types.ModuleType("langchain_mcp_adapters.tools")
        tools_module.load_mcp_tools = fake_load_mcp_tools

        servers = build_mcp_servers(
            "token-1",
            environ=(
                {"TAVILY_API_KEY": "tvly-key"} if "tavily" in results else {}
            ),
        )
        modules = {
            "mcp": mcp_module,
            "mcp.client.streamable_http": http_module,
            "langchain_mcp_adapters.tools": tools_module,
        }
        with patch.dict(sys.modules, modules):
            async with contextlib.AsyncExitStack() as stack:
                return await server.load_tools_from_mcp_servers(stack, servers)

    async def test_tools_from_both_servers_are_merged_and_prefixed(self):
        tools = await self._load({"uipath": ["search"], "tavily": ["search"]})

        self.assertEqual(
            [tool.name for tool in tools], ["uipath_search", "tavily_search"]
        )

    async def test_a_broken_uipath_server_still_leaves_the_fallback(self):
        tools = await self._load(
            {"uipath": RuntimeError("MCP server exploded"), "tavily": ["search"]}
        )

        self.assertEqual([tool.name for tool in tools], ["tavily_search"])

    async def test_a_broken_fallback_leaves_the_primary(self):
        tools = await self._load(
            {"uipath": ["search"], "tavily": RuntimeError("bad api key")}
        )

        self.assertEqual([tool.name for tool in tools], ["uipath_search"])

    async def test_a_primary_401_propagates_for_the_token_refresh(self):
        unauthorized = httpx.HTTPStatusError(
            "unauthorized",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(401),
        )

        with self.assertRaises(httpx.HTTPStatusError):
            await self._load({"uipath": unauthorized, "tavily": ["search"]})

    async def test_a_fallback_401_does_not_propagate(self):
        unauthorized = httpx.HTTPStatusError(
            "unauthorized",
            request=httpx.Request("POST", "https://example.test"),
            response=httpx.Response(401),
        )

        tools = await self._load({"uipath": ["search"], "tavily": unauthorized})

        self.assertEqual([tool.name for tool in tools], ["uipath_search"])


class V03MessageNormalizationTests(unittest.TestCase):
    def test_bare_string_content_becomes_a_text_part(self):
        normalized = normalize_v0_3_message_payload(CONNECTOR_MESSAGE_BODY)

        self.assertEqual(
            normalized["message"]["content"],
            [{"text": "latest US Open news"}],
        )

    def test_capabilities_is_kept_as_message_metadata(self):
        normalized = normalize_v0_3_message_payload(CONNECTOR_MESSAGE_BODY)

        self.assertEqual(
            normalized["message"]["metadata"],
            {"capabilities": "uipath_web_search"},
        )

    def test_missing_role_defaults_to_user(self):
        normalized = normalize_v0_3_message_payload(CONNECTOR_MESSAGE_BODY)

        self.assertEqual(normalized["message"]["role"], "ROLE_USER")

    def test_missing_message_id_is_filled_in(self):
        normalized = normalize_v0_3_message_payload(CONNECTOR_MESSAGE_BODY)

        self.assertTrue(normalized["message"]["messageId"])

    def test_json_rpc_style_parts_and_role_are_translated(self):
        normalized = normalize_v0_3_message_payload(
            {
                "message": {
                    "messageId": "m1",
                    "role": "user",
                    "parts": [{"kind": "text", "text": "hello"}],
                }
            }
        )

        self.assertEqual(normalized["message"]["role"], "ROLE_USER")
        self.assertEqual(
            normalized["message"]["content"],
            [{"kind": "text", "text": "hello"}],
        )
        self.assertNotIn("parts", normalized["message"])

    def test_a_valid_message_is_left_unchanged(self):
        message = {
            "messageId": "m1",
            "role": "ROLE_USER",
            "content": [{"text": "hello"}],
        }

        normalized = normalize_v0_3_message_payload({"message": message})

        self.assertEqual(normalized["message"], message)

    def test_a_send_defaults_to_blocking(self):
        normalized = normalize_v0_3_message_payload(CONNECTOR_MESSAGE_BODY)

        self.assertEqual(normalized["configuration"], {"blocking": True})

    def test_an_explicit_blocking_choice_is_respected(self):
        normalized = normalize_v0_3_message_payload(
            {**CONNECTOR_MESSAGE_BODY, "configuration": {"blocking": False}}
        )

        self.assertEqual(normalized["configuration"], {"blocking": False})

    def test_describe_invalid_message_names_the_empty_content_list(self):
        self.assertEqual(
            describe_invalid_v0_3_message({"message": {"content": []}}),
            "message.content must be a non-empty list of parts",
        )

    def test_describe_invalid_message_names_the_offending_part(self):
        self.assertEqual(
            describe_invalid_v0_3_message(
                {"message": {"content": [{"text": "ok"}, {"kind": "text"}]}}
            ),
            'message.content[1] must set one of "text", "file" or "data"',
        )

    def test_describe_invalid_message_accepts_a_valid_body(self):
        self.assertIsNone(
            describe_invalid_v0_3_message(
                {"message": {"content": [{"text": "hello"}]}}
            )
        )


class V03MessageSendRouteTests(unittest.TestCase):
    """The v0.3 HTTP+JSON route the UiPath A2A connector actually calls."""

    def setUp(self):
        self.received = []

        async def fake_agent_runner(user_input):
            self.received.append(user_input)
            return "Alcaraz won in four sets."

        patcher = patch(
            "server.UiPathAgentExecutor",
            lambda: UiPathAgentExecutor(fake_agent_runner),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(build_app("127.0.0.1", 9999))

    def test_connector_message_shape_reaches_the_agent(self):
        response = self.client.post("/v1/message:send", json=CONNECTOR_MESSAGE_BODY)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.received, ["latest US Open news"])

    def test_send_response_already_carries_the_finished_answer(self):
        task = self.client.post(
            "/v1/message:send", json=CONNECTOR_MESSAGE_BODY
        ).json()["task"]

        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(
            task["status"]["message"]["content"],
            [{"text": "Alcaraz won in four sets."}],
        )
        self.assertEqual(
            task["artifacts"][0]["parts"], [{"text": "Alcaraz won in four sets."}]
        )

    def test_unfixable_message_returns_400_naming_the_field(self):
        response = self.client.post("/v1/message:send", json={"message": {}})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["message"],
            "message.content must be a non-empty list of parts",
        )

    def test_malformed_json_returns_400(self):
        response = self.client.post(
            "/v1/message:send",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)

    def test_connector_task_poll_returns_the_completed_task(self):
        task_id = self.client.post(
            "/v1/message:send", json=CONNECTOR_MESSAGE_BODY
        ).json()["task"]["id"]

        response = self.client.get(f"/v1/tasks:get?id={task_id}")

        self.assertEqual(response.status_code, 200)
        task = response.json()
        self.assertEqual(task["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(
            task["status"]["message"]["content"],
            [{"text": "Alcaraz won in four sets."}],
        )

    def test_connector_task_poll_matches_the_resource_style_route(self):
        task_id = self.client.post(
            "/v1/message:send", json=CONNECTOR_MESSAGE_BODY
        ).json()["task"]["id"]

        self.assertEqual(
            self.client.get(f"/v1/tasks:get?id={task_id}").json(),
            self.client.get(f"/v1/tasks/{task_id}").json(),
        )

    def test_task_poll_without_an_id_returns_400(self):
        response = self.client.get("/v1/tasks:get")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["message"],
            'query parameter "id" is required',
        )


if __name__ == "__main__":
    unittest.main()
