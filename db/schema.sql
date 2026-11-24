-- RankForge schema. Run once in Supabase -> SQL Editor (safe to re-run).
-- Every user-owned table carries user_id; every API query filters on it.
-- LangGraph's checkpoint tables (short-term memory) are created by PostgresSaver.setup().

create table if not exists users (
    user_id     text primary key,
    display_name text,
    is_admin    boolean not null default false,
    created_at  timestamptz not null default now()
);

-- Long-term memory: per user, across conversations
create table if not exists user_memory (
    user_id    text not null references users(user_id) on delete cascade,
    key        text not null,              -- niche, site_name, tone, audience, programs ...
    value      text not null,
    updated_at timestamptz not null default now(),
    primary key (user_id, key)
);

-- Keyword Scout output
create table if not exists keywords (
    id          bigserial primary key,
    user_id     text not null references users(user_id) on delete cascade,
    keyword     text not null,
    demand      real,          -- proxy volume (autocomplete depth / wiki pageviews)
    competition real,          -- 0..1 heuristic
    intent      real,          -- 0..1 buyer-intent score
    commission  real,          -- best matching affiliate commission %
    score       real,
    status      text not null default 'new',   -- new | targeted | retired
    created_at  timestamptz not null default now(),
    unique (user_id, keyword)
);

-- Content Planner briefs + Writer/Editor drafts
create table if not exists briefs (
    id         bigserial primary key,
    user_id    text not null references users(user_id) on delete cascade,
    keyword    text not null,
    brief      jsonb not null,
    created_at timestamptz not null default now()
);

create table if not exists drafts (
    id             bigserial primary key,
    user_id        text not null references users(user_id) on delete cascade,
    brief_id       bigint references briefs(id) on delete set null,
    stage          text not null,          -- draft | linked | final
    body           text not null,
    flagged_claims jsonb not null default '[]',
    seo_report     jsonb,
    created_at     timestamptz not null default now()
);

-- Affiliate catalog (shared, not user-owned)
create table if not exists affiliate_programs (
    id             serial primary key,
    name           text not null unique,
    network        text not null,          -- Amazon Associates, CJ, ShareASale, Impact ...
    category       text not null,
    commission_pct real not null,
    base_url       text not null
);

-- Own CMS (Publisher target)
create table if not exists posts (
    id           bigserial primary key,
    user_id      text not null references users(user_id) on delete cascade,
    slug         text not null unique,
    title        text not null,
    keyword      text not null,
    meta_description text,
    html         text not null,
    schema_jsonld jsonb,
    status       text not null default 'published',  -- draft | published | archived
    rewrite_count int not null default 0,
    published_at timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

-- Tracked affiliate links (/r/{code})
create table if not exists affiliate_links (
    code        text primary key,
    user_id     text not null references users(user_id) on delete cascade,
    post_id     bigint references posts(id) on delete cascade,
    program_id  int references affiliate_programs(id),
    target_url  text not null,
    anchor_text text,
    created_at  timestamptz not null default now()
);

-- Monitor time series
create table if not exists rankings (
    id        bigserial primary key,
    user_id   text not null references users(user_id) on delete cascade,
    post_id   bigint not null references posts(id) on delete cascade,
    keyword   text not null,
    position  int not null,
    source    text not null default 'simulated',   -- simulated | gsc
    date      date not null,
    unique (post_id, keyword, date)
);

create table if not exists clicks (
    id               bigserial primary key,
    user_id          text not null references users(user_id) on delete cascade,
    post_id          bigint not null references posts(id) on delete cascade,
    date             date not null,
    sessions         int not null default 0,
    affiliate_clicks int not null default 0,
    conversions      int not null default 0,
    unique (post_id, date)
);

-- Optimizer audit trail
create table if not exists action_log (
    id         bigserial primary key,
    user_id    text not null references users(user_id) on delete cascade,
    post_id    bigint references posts(id) on delete set null,
    decision   text not null,              -- rewrite | retarget | hold
    notes      text,
    loop_count int not null default 0,
    created_at timestamptz not null default now()
);

-- Human-in-the-loop queue
create table if not exists approvals (
    id           bigserial primary key,
    user_id      text not null references users(user_id) on delete cascade,
    thread_key   text not null,            -- "{user_id}:{thread_id}"
    resume_token text not null unique,
    trigger      text not null,            -- risky_action | cost_ceiling | too_many_flags | loop_overflow
    action       jsonb not null,
    status       text not null default 'pending',  -- pending | approved | rejected | amended
    created_at   timestamptz not null default now(),
    resolved_at  timestamptz
);

-- Observability spans (one per node / LLM call)
create table if not exists spans (
    id         bigserial primary key,
    run_id     text not null,
    user_id    text,
    node       text not null,
    tokens_in  int not null default 0,
    tokens_out int not null default 0,
    cost_usd   numeric(10,6) not null default 0,
    latency_ms int,
    tool_calls jsonb not null default '[]',
    status     text not null default 'ok',   -- ok | error | degraded | refused
    error      text,
    created_at timestamptz not null default now()
);

-- Increment 9+: login accounts and account disabling (safe to re-run)
alter table users add column if not exists password_hash text;      -- null = API-only user (no login)
alter table users add column if not exists disabled boolean not null default false;

create index if not exists idx_keywords_user   on keywords(user_id);
create index if not exists idx_posts_user      on posts(user_id);
create index if not exists idx_rankings_post   on rankings(post_id, date);
create index if not exists idx_clicks_post     on clicks(post_id, date);
create index if not exists idx_spans_created   on spans(created_at);
create index if not exists idx_spans_user      on spans(user_id);
create index if not exists idx_approvals_user  on approvals(user_id, status);
create index if not exists idx_action_log_user on action_log(user_id, created_at);
create index if not exists idx_drafts_user     on drafts(user_id, stage);

-- Row-Level Security: deny-by-default for Supabase anon/authenticated roles.
-- The API connects as the postgres role (bypasses RLS) and filters by user_id itself.
-- Website read policies are added in the website step.
alter table users             enable row level security;
alter table user_memory       enable row level security;
alter table keywords          enable row level security;
alter table briefs            enable row level security;
alter table drafts            enable row level security;
alter table posts             enable row level security;
alter table affiliate_links   enable row level security;
alter table rankings          enable row level security;
alter table clicks            enable row level security;
alter table action_log        enable row level security;
alter table approvals         enable row level security;
alter table spans             enable row level security;
alter table affiliate_programs enable row level security;
