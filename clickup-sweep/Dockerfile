FROM node:20-bookworm-slim
# ClickUp card-sweep → fleet PM dispatcher. No npm deps (Node 20 built-ins only). State on /data volume.
WORKDIR /app
COPY sweep.js ./
CMD ["node", "sweep.js"]
