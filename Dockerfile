FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY flightopt ./flightopt

EXPOSE 8000

CMD ["uv", "run", "--frozen", "uvicorn", "flightopt.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

