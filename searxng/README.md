# SearXNG — Firecrawl's search backend for fleet `web_search`

Firecrawl's default search is DuckDuckGo scraping, which gets anti-bot blocked from the datacenter
IP (`DuckDuckGo: Blocked by anti-bot measures`) and returns `success:true` with 0 results — a silent
~50%+ empty rate that broke the researcher/seo agents' `web_search`.

Fix (2026-08-25): a self-hosted SearXNG instance provides `SEARXNG_ENDPOINT` to firecrawl, which
takes priority over DDG. SearXNG aggregates engines server-side and routes around blocked ones
(brave/DDG/startpage are CAPTCHA-blocked here; Google answers reliably). Result: 8/8 non-empty.

## Deployment
- Runs INSIDE the firecrawl Coolify service (`da1g1lpeilodrfywwglgasgf`) as the `searxng` service on
  the `backend` network — see `../firecrawl/compose.yml`. firecrawl `api` reaches it at
  `http://searxng:8080` via `SEARXNG_ENDPOINT`.
- settings.yml is bind-mounted read-only from the host at `/home/harsh/searxng-cfg/settings.yml`
  (NOT in git — it holds the real secret_key). `settings.yml.template` here is the redacted copy;
  regenerate the secret with `openssl rand -hex 32`.

## Critical config (or it silently fails)
- `search.formats` MUST include `json` — firecrawl queries `GET /search?...&format=json`; without it
  SearXNG returns 403 and firecrawl gets nothing.
- `server.limiter: false` — the bot/rate limiter needs Valkey and would block firecrawl's automated
  queries. Safe because the instance is internal-only (backend network, no published port).

## Verify
    docker exec searxng-<uuid> wget -qO- "http://127.0.0.1:8080/search?q=test&format=json"
    # firecrawl path, from the hermes container:
    curl -s http://api-<uuid>:3002/v1/search -H 'Content-Type: application/json' -d '{"query":"...","limit":5}'
    # confirm provider: firecrawl logs show "Using searxng search", not "Using DuckDuckGo search"
