# claude-watcher

An unofficial **RSS 2.0 + Atom feed for [anthropic.com/news](https://www.anthropic.com/news)**,
built hourly by GitHub Actions and served from GitHub Pages. No server to run or pay for.

## How it works

1. `build_feed.py` fetches Anthropic's [sitemap](https://www.anthropic.com/sitemap.xml) — the
   stable, machine-readable list of every news article (the news page itself uses hashed CSS
   class names and is brittle to scrape).
2. For each **newly-appeared** article it fetches the page once and reads the Open Graph tags
   (`og:title`, `og:description`, `og:image`).
3. Metadata + dates are persisted in `feed_state.json` (committed to the repo). Publish dates are
   **first-seen and stable** — the feed never reorders itself when Anthropic edits an old page.
   On the very first run, dates are seeded from the sitemap `<lastmod>`.
4. It writes `public/feed.xml`, `public/atom.xml`, and `public/index.html`.
5. The workflow commits any changes and deploys `public/` to GitHub Pages.

## One-time setup

1. **Create a GitHub repo** and push this project (see below).
2. **Settings → Pages → Source = _GitHub Actions_.**
3. Set your Pages URL in **two** places (trailing slash required):
   - `FEED_PAGE_URL` env in `.github/workflows/build.yml`
   - the default `FEED_PAGE_URL` in `build_feed.py` (used for local runs)

   e.g. `https://<your-user>.github.io/claude-watcher/`
4. Run the workflow once: **Actions → Build Anthropic news feed → Run workflow.** This seeds
   `feed_state.json` from the sitemap and publishes the first feed.

Your feed URLs will then be:
- RSS: `https://<your-user>.github.io/claude-watcher/feed.xml`
- Atom: `https://<your-user>.github.io/claude-watcher/atom.xml`

## Run locally

```bash
pip install -r requirements.txt
python build_feed.py            # writes feed_state.json + public/
```

## Caveats

- **Scheduled runs are best-effort.** GitHub may delay hourly crons by several minutes under load.
- **Inactivity disables schedules.** GitHub auto-disables scheduled workflows after 60 days with no
  repo activity. The hourly commit keeps the repo active, so this normally won't trigger.
- **Depends on Anthropic's markup.** If their sitemap or OG tags change, enrichment degrades
  gracefully (falls back to a title derived from the URL slug) rather than failing the run.

Not affiliated with Anthropic. Data comes from their public sitemap.
