"""A minimal A2A remote agent server.

This is a skeleton: a working A2A server that accepts a message and replies
with a canned response. There is no LLM here yet — that gets added later.

Run it:
    uv run server.py
Then fetch its public "business card":
    curl http://127.0.0.1:9999/.well-known/agent-card.json
"""

import argparse
import asyncio
import contextlib
import logging

import uvicorn
from fastapi import FastAPI

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
    Part,
    Task,
    TaskState,
    TaskStatus,
)

logger = logging.getLogger(__name__)


class HelloWorldExecutor(AgentExecutor):
    """The agent's actual logic lives here.

    For now it ignores what the user said and always replies "Hello World".
    Later, `execute` is where you'd call an LLM, tools, etc.
    """

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

        # Move the task into the "working" state.
        await updater.start_work()

        # Do the "work". This is the part you'll replace with real logic.
        user_input = context.get_user_input()
        logger.info("Received: %r", user_input)

        # Emit the result as an artifact, then mark the task complete.
        await updater.add_artifact(
            parts=[Part(text="Hello World")],
            name="response",
            last_chunk=True,
        )
        await updater.complete()

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
    base_url = f"http://{host}:{port}"
    return AgentCard(
        name="Hello World Agent",
        description="A skeleton A2A agent that always replies 'Hello World'.",
        provider=AgentProvider(organization="Playground", url="https://example.com"),
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        skills=[
            AgentSkill(
                id="hello_world",
                name="Hello World",
                description="Replies with a greeting.",
                tags=["hello"],
                examples=["hi", "hello"],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
        # How clients can actually talk to this agent. We expose two HTTP
        # transports off the same FastAPI app; no gRPC in this skeleton.
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{base_url}/a2a/jsonrpc",
            ),
            AgentInterface(
                protocol_binding="HTTP+JSON",
                protocol_version="1.0",
                url=f"{base_url}/a2a/rest",
            ),
        ],
    )


def build_app(host: str, port: int) -> FastAPI:
    """Wire the executor + agent card into a FastAPI app exposing A2A routes."""
    agent_card = build_agent_card(host, port)

    # The request handler is the bridge between the A2A protocol (incoming
    # requests) and your executor (the work). The task store tracks task state.
    request_handler = DefaultRequestHandler(
        agent_executor=HelloWorldExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=agent_card),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler=request_handler, rpc_url="/a2a/jsonrpc"
        ),
        rest_routes=create_rest_routes(
            request_handler=request_handler, path_prefix="/a2a/rest"
        ),
    )
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="Skeleton A2A agent server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9999)
    args = parser.parse_args()

    app = build_app(args.host, args.port)

    logger.info("Agent card: http://%s:%s/.well-known/agent-card.json", args.host, args.port)
    config = uvicorn.Config(app, host=args.host, port=args.port)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
