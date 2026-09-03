FROM node:20-bookworm-slim
# ClickUp <-> Webster (Hermes) chat bridge. No npm dependencies — Node 20 built-ins only
# (fetch, crypto, fs). State persists on the mounted /data volume.
WORKDIR /app
COPY bridge.js ./
CMD ["node", "bridge.js"]
