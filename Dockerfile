FROM node:22-alpine AS frontend-build
WORKDIR /build/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHADOWSYNC_DATA_DIR=/data \
    SHADOWSYNC_FRONTEND_DIST=/app/frontend/dist
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir . && \
    addgroup --system shadowsync && \
    adduser --system --ingroup shadowsync shadowsync && \
    mkdir -p /data && chown shadowsync:shadowsync /data
COPY --from=frontend-build /build/frontend/dist ./frontend/dist
USER shadowsync
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1
CMD ["uvicorn", "shadowsync.api:app", "--host", "0.0.0.0", "--port", "8000"]
