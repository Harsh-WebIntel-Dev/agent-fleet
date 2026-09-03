FROM node:20-slim
# Wraps the SAME third-party Mailchimp MCP the operator uses locally — `mailchimp-mcp@0.6.0` (a stdio
# server) — and bridges it to streamable-HTTP via supergateway (Node) so the fleet's litellm gateway can
# reach it like the other sidecars. mailchimp-mcp imports `mcp.server.fastmcp` (mcp 1.x), so we PIN
# `mcp<2` in its OWN Python venv; supergateway (Node) is fully isolated from Python's mcp versioning, which
# is why we bridge with it rather than a Python proxy. The Mailchimp API key is injected at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/mc \
 && /opt/mc/bin/pip install --no-cache-dir 'mailchimp-mcp==0.6.0' 'mcp<2'
RUN npm install -g supergateway
ENV PATH="/opt/mc/bin:${PATH}"
WORKDIR /app
COPY infisical_fetch.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
