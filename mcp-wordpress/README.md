# mcp-wordpress

Thin MCP server over the **official WordPress REST API** (`wp-json`), giving the fleet `publisher` agent an
honest, verifiable publishing surface for a client's WordPress site.

## Why this and not the Automattic `wordpress-mcp` plugin

The official `wordpress-mcp` plugin authenticates its MCP endpoint with **short-lived JWTs only** —
application-password auth does not set the user on its endpoint, so a standing fleet integration would need
a daily JWT-refresh mechanism. The standard `wp-json` REST API accepts the content-bot **application
password** directly (proven), so this wraps that instead — the same mechanism the retired marketing-agent
bridge ran in production.

## Tools

| Tool | Purpose |
|------|---------|
| `account_status` | Health/identity check (`/wp/v2/users/me`) — confirms auth + `can_publish`. |
| `wp_list_categories` | List existing categories to pick one for a draft. |
| `wp_upload_media(image_url, …)` | SSRF-safe fetch of a raster image → upload to the media library → `media_id`. |
| `wp_create_draft(title, content_html, seo_*, category, featured_media_id)` | Create a **draft** with Yoast SEO meta + category + featured image → real `post_id` + `edit_link`. |
| `wp_update_post(post_id, …)` | Apply QA/review-loop edits to a draft. |
| `wp_get_post(post_id)` | Read current post state (for review / verification). |
| `wp_publish(post_id)` | Set live **only after human approval**; independently re-fetches the URL and reports `verified_live`. |

## Anti-confabulation contract

Every returned `post_id` / `url` comes from the WordPress API response or from an independent fetch this
server performs. It never invents an id or URL; a failed call is an honest error. `wp_publish` reports
`published` (WordPress's own status) separately from `verified_live` (an independent cache-busted GET) —
a Cloudflare cache can lag a publish, and a human must purge Cloudflare.

## Configuration (env / Coolify secrets)

| Var | Default | Notes |
|-----|---------|-------|
| `WP_SITE_URL` | `https://webintelligenz.com` | Must be https. |
| `WP_USERNAME` | `content-bot` | WordPress user (id 88). |
| `WP_APP_PASSWORD` | — | **Secret.** Application password for `WP_USERNAME`. Never commit / print. |
| `WP_HTTP_TIMEOUT` | `30` | Seconds. |
| `WP_MEDIA_MAX_BYTES` | `12582912` | Hero-image fetch cap (12 MB). |

## Site-side prerequisites (one-time, webintelligenz.com)

- `content_bot` role must have `publish_posts` (granted by `scratchpad/wp-fleet-prep.sh`). It already has
  `edit_posts` / `edit_published_posts` / `upload_files`, and `assign_terms` for categories maps to
  `edit_posts`, so assigning an existing category over REST works.
- `wi-yoast-rest.php` mu-plugin exposes the 3 Yoast meta keys over REST **and** (after the prep script)
  refreshes the Yoast indexable on each write, so the rendered `<title>`/meta stay correct without WP-CLI.

## Build & deploy

Built on the server (no git remote yet), referenced by tag, deployed as a Coolify service on the
`fleet-core` project — same pattern as `mcp-higgsfield`. Internal only; registered in LiteLLM's MCP
gateway under `access_groups: [fleet_tools]`.
