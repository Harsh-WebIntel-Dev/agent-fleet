FROM python:3.12-slim
# Thin MCP over the OFFICIAL Lnk.Bio REST API -- maintains the Web Intelligenz Instagram bio page as a
# ROLLING TOP-5 of links. WEB INTELLIGENZ ONLY (not exposed to client keys) -- it's WI's own bio page.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py infisical_fetch.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
