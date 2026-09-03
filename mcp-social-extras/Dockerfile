FROM python:3.12-slim
# Thin MCP exposing the Postiz operations the official built-in MCP omits (edit/delete/status of an
# EXISTING post by id) + a Lnk.Bio "rolling top-5 links" tool. Both are plain HTTP over the vendors'
# own public REST APIs -- no reverse-engineering. Creds via env (pulled from Infisical at boot).
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py infisical_fetch.py entrypoint.sh ./
RUN chmod +x entrypoint.sh
EXPOSE 8080
ENTRYPOINT ["/app/entrypoint.sh"]
