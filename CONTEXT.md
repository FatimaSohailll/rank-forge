# RankForge — Project Context

> AI-powered SEO and affiliate content engine.
> Agents research keywords, write and fact-check articles, inject affiliate links, publish to CMS, then track rankings and clicks — looping back to re-write or re-target based on performance.

---

## Architecture: LangGraph agent graph

The system is a **directed cyclic graph** built with LangGraph. Two feedback loops make it a live engine rather than a one-shot pipeline:

- **Re-write loop** — Optimizer → Writer (poor-performing article gets a revised brief)
- **Re-target loop** — Optimizer → Keyword Scout (low-potential keyword gets replaced)

### Nodes and responsibilities

| Agent | Lane | Job |
|---|---|---|
| **Keyword Scout** | Research | Queries SEO APIs, scores keywords by volume / competition / affiliate intent, writes to Keyword DB |
| **Content Planner** | Planning | Reads scored keywords, scrapes SERP top-10, picks affiliate angle, writes structured brief |
| **Writer** | Creation | Generates full article from brief; may call web search mid-draft for supporting facts |
| **Fact Checker** | QA (parallel) | Extracts every factual claim, verifies each via web search, flags or rewrites bad claims |
| **Link Builder** | QA (parallel) | Resolves affiliate URLs, injects tracked links, matches internal link candidates |
| **Editor** | Edit | Merges fact-checked copy + linked copy, runs SEO rule checks, produces publish-ready HTML |
| **Publisher** | Publish | Pushes to CMS (WordPress / Webflow / Ghost), sets slug, schema markup, canonical, featured image |
| **Rank Tracker** | Monitor (parallel) | Polls SERP APIs on a cron schedule, writes position history to Rankings DB |
| **Click Tracker** | Monitor (parallel) | Pulls GA4 + affiliate network reports on a cron schedule, writes CTR and conversion history |
| **Optimizer** | Optimize | Reads ranking + click signals, decides: trigger re-write (→ Writer) or re-target (→ Keyword Scout) |

### Graph edges

```
Keyword Scout → Content Planner → Writer ─┬→ Fact Checker ─┐
                                           └→ Link Builder  ─┴→ Editor → Publisher ─┬→ Rank Tracker ─┐
                                                                                     └→ Click Tracker ─┴→ Optimizer
Optimizer ──(re-write)──────────────────────────────────────────────────────────────────────────────→ Writer
Optimizer ──(re-target)─────────────────────────────────────────────────────────────────────────────→ Keyword Scout
```

---

## Tools per agent

### Keyword Scout
- Semrush API — keyword volume, difficulty, CPC
- Ahrefs API — backlink gap, keyword ideas
- Google Trends — seasonality signals
- Amazon Product Advertising API — affiliate commission data
- Keyword DB (read/write) — internal store
- LLM scorer — ranks keyword clusters by monetisation potential

### Content Planner
- Keyword DB (read)
- SERP scraper — top-10 titles, headings, word counts
- Affiliate catalog — available programs and commission rates
- LLM brief generator — produces title, outline, angle, target word count
- Brief store (write)

### Writer
- Brief store (read)
- Web search — supporting research mid-draft
- Web fetch — pull specific pages for context
- LLM long-context — full article generation
- Draft store (write)

### Fact Checker _(parallel with Link Builder)_
- Draft store (read)
- Web search + web fetch — source verification
- LLM claim extractor — identifies every verifiable claim in the draft
- LLM verifier — checks each claim against retrieved sources
- Flagged claims store (write)

### Link Builder _(parallel with Fact Checker)_
- Draft store (read)
- Affiliate network APIs (Amazon, CJ, ShareASale, Impact, etc.)
- Link shortener / tracker — generates tracked redirect URLs
- Internal link index — existing published posts for cross-linking
- LLM anchor text — picks natural anchor phrases
- Linked draft store (write)

### Editor
- Flagged claims store (read)
- Linked draft store (read)
- LLM merge + polish — reconciles both QA outputs into one clean draft
- Readability scorer (Flesch-Kincaid or similar)
- SEO rule checker — keyword density, meta description length, heading hierarchy
- Final draft store (write)

### Publisher
- Final draft store (read)
- WordPress REST API / Webflow API / Ghost Admin API
- Schema markup builder — Article, Review, FAQPage JSON-LD
- Image generation API — featured image
- Published posts DB (write)

### Rank Tracker _(parallel with Click Tracker, cron-triggered)_
- Published posts DB (read)
- SEMrush / DataForSEO SERP API
- Google Search Console API
- Rankings time-series DB (write)
- Scheduler (cron)

### Click Tracker _(parallel with Rank Tracker, cron-triggered)_
- Published posts DB (read)
- GA4 / Plausible Analytics API
- Affiliate network reporting APIs
- Clicks time-series DB (write)
- Scheduler (cron)

### Optimizer
- Rankings time-series DB (read)
- Clicks time-series DB (read)
- LLM decision maker — interprets signal trends, produces action recommendation
- LangGraph router — conditional edge to Writer (re-write) or Keyword Scout (re-target)
- Action log (write) — audit trail of all optimisation decisions

---

## Shared state schema (LangGraph `TypedDict`)

```python
class RankForgeState(TypedDict):
    # Research
    keyword: str
    keyword_meta: dict          # volume, difficulty, cpc, affiliate_commission

    # Planning
    brief: dict                 # title, outline, angle, word_count_target, affiliate_programs

    # Creation
    draft: str                  # raw article HTML/markdown

    # QA
    flagged_claims: list[dict]  # [{claim, verdict, source_url}]
    linked_draft: str           # draft with affiliate + internal links injected

    # Edit
    final_draft: str            # publish-ready HTML

    # Publish
    post_id: str
    post_url: str
    published_at: str           # ISO timestamp

    # Monitor
    rankings: list[dict]        # [{date, keyword, position}]
    clicks: list[dict]          # [{date, sessions, affiliate_clicks, conversions}]

    # Optimize
    optimizer_decision: str     # "rewrite" | "retarget" | "hold"
    optimizer_notes: str        # human-readable reasoning
    loop_count: int             # guard against infinite re-write loops
```

---

## Middleware

Seven middleware layers wrap the agent graph. They run outermost-first on the way in and innermost-first on the way out — like Python decorators stacked around every node invocation.

```
Observability
  └── Auth & secrets
        └── Rate limiter
              └── Cost guard
                    └── Retry & fallback
                          └── State validator
                                └── Human-in-the-loop
                                      └── [ agent node ]
```

### 1. Observability _(outermost)_
Wraps every node. Emits a structured span per invocation: agent name, input/output token counts, latency, tool calls made, exit status. Ships to OpenTelemetry / LangSmith / Datadog. Source of truth for the ops dashboard.

- **Hooks:** `pre-node`, `post-node`, `on-error`
- **Emits:** trace span, token count, latency metric, tool call log

### 2. Auth & secrets
Injects API credentials into the execution context before any tool call. Reads from a secrets backend (Vault / AWS Secrets Manager / env). Rotates short-lived tokens. No agent ever hardcodes a credential — they always pull from context.

- **Hooks:** `graph-start`, `pre-tool-call`
- **Emits:** credential context, token refresh event

### 3. Rate limiter
Enforces two budgets: (a) LLM tokens-per-minute per model tier, (b) external API call quotas (Semrush, Ahrefs, affiliate APIs). Queues excess calls with jitter rather than dropping them. Prevents cascading 429s from killing a run mid-article.

- **Hooks:** `pre-tool-call`
- **Emits:** queue delay event, quota usage metric
- **Config:** `tpm_limit` per model, `api_rpm` per integration

### 4. Cost guard
Tracks cumulative spend per pipeline run in real time against a configurable ceiling (e.g. `$0.50` per article). If the ceiling is hit mid-run, halts the graph, writes current state to the checkpoint store, and fires a Human-in-the-loop interrupt. Each retry attempt re-runs the cost check — retries cannot silently push the run over budget.

- **Hooks:** `post-llm-call`, `post-tool-call`
- **Emits:** spend event, ceiling-hit interrupt
- **Config:** `max_spend_per_run`, `warn_at_pct` (e.g. 80%)

### 5. Retry & fallback
Wraps every LLM call and tool call. On transient errors (timeout, 429, 503) retries with exponential backoff + jitter (max 3 attempts). On persistent model failure swaps to a fallback model (e.g. primary → secondary tier). On tool failure marks the result as unavailable and lets the agent continue with degraded context rather than crashing the run.

- **Hooks:** `on-llm-error`, `on-tool-error`
- **Emits:** retry event, fallback event, degraded-context flag
- **Config:** `max_retries`, `fallback_model_map`

### 6. State validator
Runs a Pydantic schema check on `RankForgeState` before a node reads it and after a node writes it. Catches missing required fields, type mismatches, or malformed URLs early — before they silently corrupt downstream agents. On failure emits a validation error with the exact field path and routes to the error handler rather than passing bad state forward.

- **Hooks:** `pre-node`, `post-node`
- **Emits:** validation pass event, validation error + field path

### 7. Human-in-the-loop _(innermost)_
Pauses the graph and fires a notification (Slack / email / webhook) in three situations:

| Trigger | Condition |
|---|---|
| Too many flagged claims | `len(flagged_claims) > max_flags` |
| Cost ceiling hit | Fired by Cost Guard middleware |
| Loop overflow | `loop_count > max_loops` |

Waits for a human approve / reject / amend response before resuming. Resume token stored in state so the graph picks up exactly where it paused.

- **Emits:** pause event + context snapshot, notification, resume token
- **Config:** `max_flags`, `max_loops`, `notify_channel`

---

## Key design decisions

1. **Fact Checker and Link Builder run in parallel** — both read the same `draft`; Editor merges their outputs. Saves wall-clock time on the QA pass.

2. **Rank Tracker and Click Tracker run on a cron**, not in the main pipeline — they write to time-series DBs continuously. Optimizer reads those DBs when triggered.

3. **Two LLM tiers** — Writer uses a large long-context model. All other LLM calls (scoring, claim extraction, anchor text, decision making) use a smaller, faster model to control cost.

4. **`loop_count` in state** — Optimizer must check this before routing back to Writer or Keyword Scout to prevent runaway loops. Set a max (e.g. 3 rewrites) before escalating to human review.

5. **Affiliate tools appear in three places** — Scout (find high-commission targets), Link Builder (inject tracked URLs), Click Tracker (pull conversion reports). This closes the full affiliate revenue loop.

6. **CMS is pluggable** — Publisher abstracts over WordPress / Webflow / Ghost via a common adapter interface. Add new targets without touching the graph.

---

## Repo structure (suggested)

```
rankforge/
├── agents/
│   ├── keyword_scout.py
│   ├── content_planner.py
│   ├── writer.py
│   ├── fact_checker.py
│   ├── link_builder.py
│   ├── editor.py
│   ├── publisher.py
│   ├── rank_tracker.py
│   ├── click_tracker.py
│   └── optimizer.py
├── graph.py           # LangGraph graph definition + edges
├── state.py           # RankForgeState TypedDict
├── tools/
│   ├── seo_apis.py    # Semrush, Ahrefs, DataForSEO wrappers
│   ├── cms.py         # WordPress / Webflow / Ghost adapters
│   ├── affiliate.py   # Amazon PA, CJ, ShareASale wrappers
│   ├── analytics.py   # GA4, Plausible, GSC wrappers
│   └── web.py         # search + fetch utilities
├── middleware/
│   ├── observability.py   # OpenTelemetry spans, token + latency metrics
│   ├── auth.py            # secrets injection, credential context
│   ├── rate_limiter.py    # TPM + API RPM budgets, jitter queue
│   ├── cost_guard.py      # per-run spend ceiling, checkpoint on halt
│   ├── retry.py           # exponential backoff, model fallback
│   ├── state_validator.py # Pydantic pre/post-node schema checks
│   └── human_in_loop.py   # pause, notify, resume token
├── stores/
│   └── ...            # DB models for keyword, brief, draft, rankings, clicks
├── RANKFORGE_CONTEXT.md   ← you are here
└── README.md
```

---

_Last updated: 2026-11-23 — added middleware definitions_