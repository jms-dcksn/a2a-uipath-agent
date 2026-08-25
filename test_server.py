import unittest
from unittest.mock import patch

import httpx
from a2a.types import Message, Part, Role, Task, TaskState
from fastapi.testclient import TestClient

from server import (
    DEFAULT_MODEL_NAME,
    ExternalAppTokenProvider,
    UiPathAgentExecutor,
    build_app,
    build_chat_model,
    build_agent_card,
    build_token_provider_from_env,
    is_unauthorized_error,
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


if __name__ == "__main__":
    unittest.main()
