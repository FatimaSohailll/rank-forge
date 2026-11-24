# Deploy RankForge to Vercel

This deploys the complete FastAPI website and streaming agent API as one Python 3.12 function.
The production branch is `deploy/vercel`; the existing Supabase database is reused without seeding or migrations.

## 1. Import the GitHub repository

1. Open https://vercel.com/new and sign in with the account that should own the demo.
2. Grant Vercel access to `FatimaSohailll/rank-forge`, then import it. You need repository access through
   Vercel's GitHub integration; a public repository alone does not grant automatic deployment access.
3. Use project name `rank-forge` if available (otherwise keep Vercel's suggested name).
4. Set **Framework Preset: FastAPI**, **Root Directory: `./`**. Keep the install/build defaults and do not
   set a frontend output directory. The repository supplies its build command automatically. The root `app.py`, `requirements.txt`, and `.python-version` configure Python.
5. Add the environment variables below before deploying. If the import initially uses the default Git branch,
   first finish creating the project, change its Production Branch to `deploy/vercel` under Settings →
   Environments → Production → Branch Tracking (or the Git settings in older dashboards), then deploy that branch.
   Only the deployment built from `deploy/vercel` is the release to validate.

## 2. Environment variables

Use Vercel's environment-variable interface. Copy secrets directly from your local configuration;
never commit them or paste them into an issue, README, screenshot, or build command.
Set runtime variables for **Production**. Preview deployment checks may use the same database only when
intended: previews share real accounts/articles and incur the same API spend. If enabled, give Preview
its own session secret and the same production public URL.

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Existing Claude key |
| `TAVILY_API_KEY` | Existing Tavily key |
| `DATABASE_URL` | Existing Supabase transaction pooler connection string, port 6543 |
| `SESSION_SECRET` | One stable, newly generated random secret; keep it unchanged between redeploys |
| `COOKIE_SECURE` | `true` |
| `MODEL_ID` | `claude-haiku-5-5` |
| `MAX_SPEND_PER_RUN_USD` | `0.02` |
| `DAILY_SPEND_CEILING_USD` | `1.50` |

Generate the session secret locally and copy its output directly into Vercel:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Leave `ADMIN_PASSWORD` unset: existing shared-database accounts retain their current passwords.
Leave `CRON_SECRET` unset for the first deployment; monitoring stays disabled.
Do not import the entire local `.env`: it contains localhost URLs and local cookie settings.

Vercel provides `VERCEL=1` and, with system environment variables exposed, `VERCEL_PROJECT_PRODUCTION_URL`.
The app derives its initial HTTPS public URL and origins from that production domain. Ensure
**Automatically expose System Environment Variables** is enabled in the project settings.
If the production-domain variable is unavailable, explicitly set the two URL variables below before deploying.

After Vercel assigns the production domain, set both variables and redeploy:

| Variable | Example (replace with your actual production domain) |
|---|---|
| `PUBLIC_SITE_URL` | `https://rank-forge.vercel.app` |
| `ALLOWED_ORIGINS` | `https://rank-forge.vercel.app` |

The URL must be the stable production domain, rather than a single deployment's generated preview URL.
Production startup rejects missing database/session configuration, insecure cookies, and local or non-HTTPS URLs.

## 3. Check the release

- Confirm the deployment uses the expected `deploy/vercel` commit and the function duration is 300 seconds.
  Fluid compute must be enabled; the Hobby maximum is 300 seconds.
- Open `/health`, `/`, `/login`, `/signup`, `/static/app.css`, and `/static/app.js`.
  Confirm the blue theme and that unauthenticated `/app` redirects to login.
- Log in with an existing account. Verify its dashboard, saved drafts, and article previews load.
- Generate one article within the spending limits. Verify progress arrives incrementally and the finished
  article shows Publish and Continue editing. Request a revision, then Publish → Back to editing.
- Publish only an article you intend to make public: Publish → Confirm and publish. Verify its live URL,
  affiliate redirects, sitemap, and canonical URL all refer to the production domain.
- Open a new browser session, log in again, and verify the saved draft persists. Check sessions/drafts again
  after a redeploy to exercise persistence across function restarts.
- Inspect Vercel runtime logs for database/import errors, timeouts, and failed requests. A function may run
  up to five minutes, but the existing model spend ceilings still apply. Streaming does not extend the limit.
- Add the verified production URL to the README. Do not mark the deployment verified before these checks pass.

## Operations

Application state and checkpoints live in Supabase; no deployment-local persistence is required.
Templates and static assets are included in the Python bundle and served by the existing FastAPI routes.
The build also bundles the NLTK pronunciation dictionary used by textstat, so SEO readability checks do not
try to download files into Vercel’s read-only runtime filesystem.
No frontend build, Docker image, or separate backend service is needed.

The existing daily GitHub monitor workflow remains disabled while its repository secrets are unset.
Enable it later by setting `RANKFORGE_URL` and `CRON_SECRET` in GitHub and the same `CRON_SECRET` in Vercel.
Per-process rate limits and Tavily caps retain their existing demo limitations across scaled instances.

Keep Docker deployment support for local testing or another host. If a release fails, inspect its build/runtime
logs, correct configuration, and redeploy the branch; for an existing live project, roll back to the last
working Vercel deployment. Do not reinitialize or reset the shared database to troubleshoot a deployment.

References: [FastAPI](https://vercel.com/docs/frameworks/backend/fastapi),
[Python runtime](https://vercel.com/docs/functions/runtimes/python),
[function duration](https://vercel.com/docs/functions/configuring-functions/duration).
