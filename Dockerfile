FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY flightopt ./flightopt

EXPOSE 8000

CMD ["uv", "run", "--frozen", "uvicorn", "flightopt.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
