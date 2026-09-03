FROM node:20-bookworm-slim
# Python (for the MCP server) + the official Higgsfield CLI (the render engine + token refresh).
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g @higgsfield/cli@1.1.23
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt
COPY server.py entrypoint.sh infisical_fetch.py ./
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
