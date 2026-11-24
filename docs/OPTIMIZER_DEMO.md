# Optimizer demo video

Use the local script to add clearly labelled simulated history to one published article in the same
Supabase database used by Vercel. No Vercel job, simulated HTTP traffic, schema migration, or model call is needed.
The script leaves article content and today's real traffic/rankings unchanged.

## Select and preview

From the repository root, with the existing `DATABASE_URL` in your environment or `.env`:

```bash
.venv/bin/python scripts/simulate_performance.py --user YOUR_USERNAME --list
.venv/bin/python scripts/simulate_performance.py --user YOUR_USERNAME --post ARTICLE_ID --scenario rewrite
```

Replace `YOUR_USERNAME` with the login username, and `ARTICLE_ID` with a published article ID returned by
`--list`. The second command is a dry run: it shows proposed dates, row counts, visits, clicks, and the
recommendation calculated by the real optimizer. It does not change the database.

Choose one scenario:

| Scenario | Simulated trend | Expected recommendation |
|---|---|---|
| `rewrite` (default) | Ranking slips from about #12 to #22; visits fall | Refresh the article |
| `retarget` | Ranking stays around #55 with little traffic | Find an easier keyword |
| `hold` | Ranking improves from about #18 to #8 | Leave the article alone |

The script creates 28 historical days ending yesterday. Today's real data is included in its recommendation
preview but never modified. Traffic includes visits and affiliate clicks (about 8% of visits); simulated
conversions stay zero. Recent totals cover six simulated days plus any actual traffic today, matching the
app's real seven-day window.

## Apply and record

After checking the article and dry-run output:

```bash
.venv/bin/python scripts/simulate_performance.py --user YOUR_USERNAME --post ARTICLE_ID --scenario rewrite --apply
```

1. Log in to Vercel as that user and refresh the dashboard. The scenario data is stored in shared Supabase,
   so no app redeployment is needed for the metrics. The latest app deployment is needed for the improved labels.
2. Show the demo-data notice, article visits/clicks, ranking change, recommendation, and explanation.
3. For Rewrite, click **Rewrite** to generate a stronger draft. Show progress, the SEO score, and the saved draft.
4. Show **Publish → Back to editing**, or request a revision with **Continue editing**.
5. If you intend to change the public article, use **Publish → Confirm and publish**. This replaces the existing
   article at the same URL; the simulator itself never publishes or rewrites content.

Generation and keyword research use the normal API keys and spending limits. Seeding metrics and viewing
the dashboard make no model calls. Keep the simulation labelled in the recording rather than presenting it
as actual search results, visits, or affiliate revenue.

## Safeguards and reuse

- Only the specified user's published article is eligible. Use a fresh demo article with no historical data.
- Existing history must exactly match this scenario, including simulated ranking provenance. Conflicting
  metrics are refused; there is no overwrite or force flag. Use another fresh article to show another scenario.
- Writes run in one transaction. A conflict or failed insert rolls back all new rows.
- Identical reruns on the same day add no duplicate rows. This is a one-time tool, not a daily refresh job;
  running it on another day can conflict with the previous scenario and will be refused.
- Combined real/simulated metrics and the article's rewrite limit are checked before any write. If they would
  produce a different recommendation or require human escalation, the script refuses that scenario.
- Other articles, accounts, today's metrics, passwords, and existing content are not modified.
- The simulated rows persist and age out of the dashboard's 30-day window naturally. No cleanup command is
  provided; do not delete shared metrics or re-run the database initialization script for this recording.
