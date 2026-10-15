# Casino bonus scraper

## Run it

```bash
pip install -r requirements.txt
$env:GEMINI_API_KEY="your-replacement-key" # Windows PowerShell
# Optional model override:
# $env:GEMINI_MODEL="gemini-3.8-flash"
# Optional timeout override, in seconds:
# $env:GEMINI_TIMEOUT_SECONDS="180"
python scraper.py                   # writes bonuses.json
python -m http.server               # open http://localhost:8000
```

## Publish the page for free

The page is static: publish `index.html` and `bonuses.json` together. For GitHub Pages,
create a repository, push these files to its `main` branch, then enable **Settings >
Pages > Deploy from a branch > main / root**. GitHub will provide the public HTTPS URL.
The browser must load the page through a server; opening `index.html` directly as a
`file://` URL will not allow `fetch("bonuses.json")` in every browser.

How it works: the script reads one public comparison page from `sources.json`, checks its
`robots.txt`, fetches the visible text, and asks a Gemini model to return exactly three casino
records. Each record contains the welcome bonus, wagering multiplier, minimum deposit, and the
source URL. Currency is detected from the offer text and stored separately. A validation step
checks that every number returned by the model appears on the source page; anything that does not
is flagged `needs_review` instead of being silently trusted. On a block (403/429), CAPTCHA page,
robots.txt disallow, or empty JS-only page, it stops and tells you to switch source. It never logs
in or works around a block.

---

## Taking this to production: 200 casinos, updated daily

I would run the application on a small Hetzner Cloud VPS in an EU region, using Ubuntu LTS and
Docker Compose. One 4-vCPU/8-GB instance is enough for 200 daily pages and keeps the architecture
simple; I would move the database to a managed service when availability or team size justified
it. The stack would contain the static web app, a Python scraper worker, PostgreSQL, and Caddy.
Deployments would run through GitHub Actions over SSH, with secrets stored in the CI secret store
and the server environment rather than in the repository.

PostgreSQL would hold `casinos`, `bonus_snapshots`, and `scrape_runs`. Every snapshot would include
the fetch time, source URL, raw-page hash, extracted fields, model metadata, and validation status.
That gives us history, auditability, and a way to skip the LLM call when a page has not changed.
Nightly encrypted `pg_dump` files would be uploaded to Backblaze B2 with 30-day retention, plus
weekly VPS snapshots. I would test a restore into a temporary container every month.

I would register the domain through Cloudflare, use Cloudflare DNS and proxying, and terminate
HTTPS with Caddy. Caddy automatically obtains and renews Let's Encrypt certificates. I would use
Full (strict) TLS, HSTS, SSH keys only, and a firewall exposing only ports 80 and 443 (plus restricted
SSH administration).

A systemd timer would start the scraper at 03:00 UTC daily. The worker would use per-host delays,
honour robots.txt, record each source result, and fail the run if the error or data-quality
threshold is exceeded. Healthchecks.io would receive start, success, and failure pings; Sentry
would capture exceptions; and UptimeRobot would monitor the public site. Alerts would cover a
missing run, more than 5% failed sources, unexpected null fields, or implausible bonus changes.
The last known-good record would remain visible but become stale after 48 hours.

Estimated monthly cost: €15–25 for the VPS, €1–3 for backups, about €1 for the domain, and roughly
$5–20 for LLM calls after hashing unchanged pages. Cloudflare, Healthchecks.io, Sentry, and
UptimeRobot can start on free tiers, giving a total of approximately **€25–45 per month**.
