# ---- Stage 1: Build Next.js dashboard ----
FROM node:20-alpine AS dashboard
WORKDIR /dashboard
COPY dashboard/package*.json ./
RUN npm ci --prefer-offline
COPY dashboard/ ./
RUN npm run build

# ---- Stage 2: Python dependencies (cached layer) ----
FROM python:3.11-slim AS base
WORKDIR /app
COPY pyproject.toml ./
# Stub engine package so pip caches deps independently of source changes
RUN mkdir engine && touch engine/__init__.py && \
    pip install --no-cache-dir . && \
    rm -rf engine

# ---- Stage 3: Test runner (CI only, not shipped) ----
FROM base AS test
RUN pip install --no-cache-dir ".[dev]"
COPY engine/ ./engine/
COPY tests/ ./tests/
ENV PYTHONPATH=/app \
    USE_TEST_ACCOUNTS=true \
    WALLET_MNEMONIC="test test test test test test test test test test test junk"
RUN pytest -x -q --ignore=tests/test_dex_fork.py

# ---- Stage 4: Production image ----
FROM base AS production
COPY engine/ ./engine/
COPY scripts/ ./scripts/
COPY --from=dashboard /dashboard/out ./dashboard/out
RUN mkdir -p data
ENV PYTHONPATH=/app
EXPOSE 8000
CMD ["python", "-m", "engine.main"]
