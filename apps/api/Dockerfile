FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    CRAWL4AI_ENABLED=false

WORKDIR /app

LABEL org.opencontainers.image.source="https://github.com/jamezhuang/followup-health-agent"

COPY requirements*.txt ./

RUN python -m pip install --no-cache-dir \
        -r requirements.txt \
    && python -m pip check \
    && useradd --create-home --uid 10001 carebrief

COPY --chown=carebrief:carebrief app/ ./app/
COPY --chown=carebrief:carebrief scripts/ ./scripts/
COPY --chown=carebrief:carebrief data/knowledge.json ./data/knowledge.json

RUN mkdir -p /app/.cache \
    && chown -R carebrief:carebrief /app/.cache /app/data

USER carebrief
EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.v2_main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--root-path", "/api"]
