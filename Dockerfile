FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py ./

EXPOSE 8080

CMD ["uv", "run", "--frozen", "--no-dev", "server.py", "--host", "0.0.0.0", "--port", "8080"]
