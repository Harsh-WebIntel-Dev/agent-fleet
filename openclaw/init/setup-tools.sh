#!/bin/sh
# Runs on every container boot via OPENCLAW_DOCKER_INIT_SCRIPT — a supported entrypoint hook, so
# no custom image is needed.
#
# Makes the Higgsfield CLI available to agents. We use the VENDOR'S CLI rather than wrapping their
# REST API: Higgsfield ships and maintains it, so endpoint changes are their problem, not a wrapper
# we have to watch. Their official MCP server is OAuth-only (authorization_code, no
# client_credentials), which issues a per-user token a machine key can never use — the same shape
# that blocked SEMrush's OAuth path.
#
# WHY THIS SCRIPT EXISTS AT ALL: npm installs to /data/npm-global (persistent) but the credential
# path and PATH do not survive a container recreate on their own.
set -eu

NPM_PREFIX=/data/npm-global
CRED_DIR=/data/.config/higgsfield          # persistent; /root is an image layer and is lost
export PATH="$NPM_PREFIX/bin:$PATH"

# ---- CLI -------------------------------------------------------------------------------------
if ! command -v higgsfield >/dev/null 2>&1; then
  echo "[tools] installing @higgsfield/cli"
  npm config set prefix "$NPM_PREFIX" >/dev/null 2>&1 || true
  npm i -g @higgsfield/cli >/dev/null 2>&1 \
    || echo "[tools] WARNING: higgsfield CLI install failed (agents will report it unavailable)"
else
  echo "[tools] higgsfield CLI present: $(higgsfield version 2>/dev/null | head -1)"
fi

# ---- credentials -----------------------------------------------------------------------------
# The CLI resolves credentials from $HOME/.config/higgsfield. HOME differs between the gateway
# process (/data) and an exec session (/root), so link both at the one persistent location rather
# than copying — a copy would drift once the CLI refreshes its token.
if [ -f "$CRED_DIR/credentials.json" ]; then
  chmod 600 "$CRED_DIR"/*.json 2>/dev/null || true
  for h in /root /data; do
    mkdir -p "$h/.config" 2>/dev/null || true
    if [ "$h/.config/higgsfield" != "$CRED_DIR" ]; then
      rm -rf "$h/.config/higgsfield" 2>/dev/null || true
      ln -s "$CRED_DIR" "$h/.config/higgsfield" 2>/dev/null || true
    fi
  done
  echo "[tools] higgsfield credentials linked from $CRED_DIR"
else
  echo "[tools] NOTE: no higgsfield credentials at $CRED_DIR — run 'higgsfield auth login' on the"
  echo "[tools]       host (callback port 8765, tunnelled) and copy the result there."
fi

# Put the CLI on PATH for every shell, including the agent exec tool.
echo "export PATH=\"$NPM_PREFIX/bin:\$PATH\"" > /etc/profile.d/fleet-tools.sh
chmod +x /etc/profile.d/fleet-tools.sh

echo "[tools] ready"
