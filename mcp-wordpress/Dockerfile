FROM python:3.12-slim
# Thin MCP over the OFFICIAL WordPress REST API (wp-json). No CLI, no plugin -- just HTTP + app-password
# Basic auth supplied via env at runtime.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py infisical_fetch.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
