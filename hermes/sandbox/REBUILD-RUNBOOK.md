# Rebuilding `hermes-sandbox` (and why it needs a runbook)

The sandbox image is the only place the fleet's HTML→PNG render capability lives. Rebuilding it is
**not** a plain Coolify redeploy: the container's network link to `hermes-agent` is provisioned by
`setup-sandbox.sh` step 4 and is **not Coolify-managed**, so a recreate silently drops it and the
producer loses `terminal` / `code_execution` entirely (CLAUDE.md §12, §15).

This runbook exists because the brand fonts added to the `Dockerfile` on 2026-09-08 (Jost, Open Sans
— see PR #15) only take effect on a rebuild. They were **also installed into the running container**
at the time, so the fix is live; a rebuild makes it durable across the next recreate.

## Preconditions — do not skip

1. **No producer card mid-render.** A render takes minutes and dies with the container.
   ```bash
   C=$(docker ps --format '{{.Names}}' | grep '^hermes-agent')
   docker exec -u hermes "$C" /opt/hermes/.venv/bin/hermes cron runs        # nothing in flight
   docker exec -u sandbox hermes-sandbox pgrep -a chromium                  # must be empty
   ```
   Also check the board for `in progress` producer cards before proceeding.

2. **prod-2 has capacity.** As of 2026-09-08 the box was memory-exhausted (swap 100% consumed, load
   sustained above 200) and a Chromium render could not complete at all. Wait for
   `load average` under ~20 and a few GB of free memory:
   ```bash
   uptime; free -m | head -3
   ```

3. **Have the R2 token to hand.** It is staged at recreate via `R2_CREDS_FILE`, never baked
   (bucket-scoped + IP-filtered to prod-2). Without it the sandbox loses its only outbound
   capability.

## Sequence

```bash
# 1. rebuild the image (Coolify service for hermes-sandbox, or locally from hermes/sandbox/)
#    the Dockerfile asserts both brand fonts resolve, so a font regression fails the BUILD.

# 2. restore the network link — MANDATORY after any recreate
./hermes/setup-sandbox.sh          # step 4 wires hermes-agent -> hermes-sandbox
#    NEVER connect the webui to the sandbox's own network (breaks Traefik -> webui outage)

# 3. re-provide the R2 credentials
#    R2_CREDS_FILE=... ./hermes/setup-sandbox.sh   (see the script for the exact variable)
```

## Verification — all four must pass

```bash
# fonts present and resolving to the REAL family (fc-match always answers; check the name)
docker exec -u sandbox hermes-sandbox fc-match Jost                 # -> "Jost"
docker exec -u sandbox hermes-sandbox fc-match Jost:weight=bold     # -> "Jost" "Bold"
docker exec -u sandbox hermes-sandbox fc-match "Open Sans"          # -> "Open Sans"

# render path end to end, and LOOK at the PNG - do not trust the exit code
docker exec -u sandbox hermes-sandbox bash -lc '
  cd /tmp && rclone copyto r2:fleet-clients/clients/webintelligenz/social/fb.html fb.html &&
  render-card fb.html /tmp/v.png 1200 630 2'
# expect: "render-card: font Jost  system", "font Open Sans  system", then "OK /tmp/v.png 2400x1260"

# R2 capability restored
docker exec -u sandbox hermes-sandbox rclone lsf r2:fleet-clients/clients/ | head

# the agent can actually reach the sandbox again
docker exec -u hermes "$C" /opt/hermes/.venv/bin/hermes -q 'run `date` in your terminal'
```

If `render-card` prints `font Jost  REMOTE-ONLY` the baked fonts did not survive the build — the
render will still work off Google Fonts, but the durability fix did not land. If it prints
`REFUSING — no source for font(s)` the fonts are gone *and* the template lost its remote stylesheet;
fix before letting any producer card run.
