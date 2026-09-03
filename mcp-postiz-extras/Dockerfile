FROM python:3.12-slim
# Thin MCP over the OFFICIAL Postiz public REST API for the post-management ops the built-in Postiz MCP
# omits (list / delete / set-status / edit an EXISTING post by id). Shared across ALL clients (fleet_tools).
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py infisical_fetch.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
