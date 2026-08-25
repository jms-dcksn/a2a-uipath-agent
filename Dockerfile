FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py request_logging.py ./

EXPOSE 8080

# Run the venv interpreter directly. "uv run" re-syncs the environment and
# recompiles bytecode for 5000+ files on every boot, which pushed start-up
# past the Fly proxy's connect timeout and returned 502 on cold starts.
CMD ["/app/.venv/bin/python", "server.py", "--host", "0.0.0.0", "--port", "8080"]
