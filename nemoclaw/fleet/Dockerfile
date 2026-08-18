# NemoClaw orchestrator — ONE service, one A2A endpoint per agent.
FROM python:3.12-slim

# curl is kept deliberately: the healthcheck needs an HTTP client in-image. The LiteLLM service
# next door reports "unhealthy" for 250 consecutive checks purely because its image has no wget
# and its healthcheck calls one. Don't repeat that here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY orchestrator/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Config (agents/pipelines/schemas/prompts) ships with the image so a rollback rolls back the
# fleet definition too, rather than leaving new prompts against old code.
COPY agents.yaml pipelines.yaml schemas.yaml /app/fleet/
COPY prompts /app/fleet/prompts
COPY orchestrator /app/fleet/orchestrator

ENV NEMOCLAW_FLEET_DIR=/app/fleet \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=8s --start-period=40s --retries=5 \
  CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "fleet.orchestrator.app:app", "--host", "0.0.0.0", "--port", "8000"]
