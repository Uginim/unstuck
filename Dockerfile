FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=UTC \
    PATH="/app/.venv/bin:$PATH" UNSTUCK_DB=/app/data/unstuck.db \
    UNSTUCK_LOG=/app/data/unstuck.jsonl UNSTUCK_MODE=public
RUN pip install --no-cache-dir uv==0.8.4
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY daybreak/ ./daybreak/
COPY unstuck/ ./unstuck/
RUN useradd --create-home appuser && mkdir -p /app/data && chown appuser:appuser /app/data
USER appuser
EXPOSE 10000
CMD ["sh", "-c", "exec uvicorn unstuck.app:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
