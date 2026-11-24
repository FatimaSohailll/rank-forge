"""Seed one published article's optimizer demo history; dry-run unless --apply.

Run from the repo root: .venv/bin/python scripts/simulate_performance.py --help
No model calls, web traffic, schema changes, or writes to other articles.
"""
import argparse
from datetime import date, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'api'))

from agents import click_tracker, optimizer, rank_tracker  # noqa: E402
from config import settings  # noqa: E402

SCENARIOS = {
    'rewrite': {'prior_rank': 12, 'recent_rank': 22, 'prior_visits': 100, 'recent_visits': 50},
    'retarget': {'prior_rank': 55, 'recent_rank': 55, 'prior_visits': 10, 'recent_visits': 10},
    'hold': {'prior_rank': 18, 'recent_rank': 8, 'prior_visits': 60, 'recent_visits': 100},
}


def plan_simulation(post, rankings, clicks, scenario='rewrite', today=None):
    """Pure validation/preview. Refuse conflicts before the caller writes anything."""
    today = today or date.today()
    if not post or post['status'] != 'published':
        raise ValueError('Choose a published article belonging to the supplied user.')
    pattern = SCENARIOS[scenario]
    proposed_rankings, proposed_clicks = [], []
    for age in range(1, 29):
        day = today - timedelta(days=age)
        window = 'recent' if age < 7 else 'prior'
        visits = pattern[f'{window}_visits']
        proposed_rankings.append(dict(post_id=post['id'], keyword=post['keyword'], date=day,
                                      position=pattern[f'{window}_rank'], source='simulated'))
        proposed_clicks.append(dict(post_id=post['id'], date=day, sessions=visits,
                                   affiliate_clicks=round(visits * .08), conversions=0))
    days = {r['date'] for r in proposed_rankings}
    expected_rank = {r['date']: r for r in proposed_rankings}
    expected_click = {r['date']: r for r in proposed_clicks}
    rank_fields = ('post_id', 'keyword', 'date', 'position', 'source')
    click_fields = ('post_id', 'date', 'sessions', 'affiliate_clicks', 'conversions')
    existing_rank, existing_click = {}, {}
    for rows, expected, fields, found in ((rankings, expected_rank, rank_fields, existing_rank),
                                           (clicks, expected_click, click_fields, existing_click)):
        for row in rows:
            if row['post_id'] != post['id']:
                raise ValueError('Input metrics must belong to the selected article only.')
            day = row['date']
            if day in days:
                if day in found or any(row.get(k) != expected[day][k] for k in fields):
                    raise ValueError(f'Conflicting historical data on {day}; nothing will be overwritten.')
                found[day] = row
    # A matching click count alone is not proof of simulated provenance.
    if any(day not in existing_rank for day in existing_click):
        raise ValueError('Existing traffic has no matching simulated ranking; nothing will be overwritten.')
    new_rankings = [r for r in proposed_rankings if r['date'] not in existing_rank]
    new_clicks = [r for r in proposed_clicks if r['date'] not in existing_click]
    since = today - timedelta(days=29)
    merged_rankings = [r for r in rankings if since <= r['date'] <= today] + new_rankings
    merged_clicks = [r for r in clicks if since <= r['date'] <= today] + new_clicks
    ranks = rank_tracker.summarise(sorted(merged_rankings, key=lambda r: r['date']), today)
    traffic = click_tracker.summarise(merged_clicks, today)
    rank = ranks[0] if ranks else {}
    traffic = traffic[0] if traffic else {}
    decision = optimizer.decide(rank, traffic, post.get('rewrite_count') or 0)
    if decision['decision'] != scenario or decision['escalate']:
        raise ValueError('Combined history/rewrites would not produce the requested scenario: ' + decision['reason'])
    return dict(rankings=new_rankings, clicks=new_clicks, rank=rank, traffic=traffic, decision=decision,
                start=today - timedelta(days=28), end=today - timedelta(days=1))


def apply_plan(conn, user, plan):
    """Insert-only; caller owns the transaction. Concurrent conflicts roll back the entire operation."""
    for r in plan['rankings']:
        conn.execute('insert into rankings (user_id, post_id, keyword, date, position, source) '
                     'values (%s,%s,%s,%s,%s,%s)',
                     (user, r['post_id'], r['keyword'], r['date'], r['position'], r['source']))
    for r in plan['clicks']:
        conn.execute('insert into clicks (user_id, post_id, date, sessions, affiliate_clicks, conversions) '
                     'values (%s,%s,%s,%s,%s,%s)',
                     (user, r['post_id'], r['date'], r['sessions'], r['affiliate_clicks'], r['conversions']))


def run(conn, args):
    """Read, validate, and optionally insert inside a single database transaction."""
    with conn.transaction():
        if args.list:
            rows = conn.execute('select id, title, keyword from posts where user_id=%s and status=%s order by id',
                                (args.user, 'published')).fetchall()
            for row in rows:
                print(f"{row['id']}: {row['title']} | {row['keyword']}")
            if not rows:
                print('No published articles for this user.')
            return
        post = conn.execute('select id, user_id, title, keyword, status, rewrite_count from posts '
                            'where user_id=%s and id=%s' + (' for update' if args.apply else ''),
                            (args.user, args.post)).fetchone()
        if not post or post['status'] != 'published':
            raise ValueError('Choose a published article belonging to the supplied user.')
        today = date.today()
        since = today - timedelta(days=29)
        ranks = conn.execute('select post_id, keyword, date, position, source from rankings '
                             'where user_id=%s and post_id=%s and date >= %s order by date',
                             (args.user, args.post, since)).fetchall()
        clicks = conn.execute('select post_id, date, sessions, affiliate_clicks, conversions from clicks '
                              'where user_id=%s and post_id=%s and date >= %s order by date',
                              (args.user, args.post, since)).fetchall()
        plan = plan_simulation(post, ranks, clicks, args.scenario, today)
        print(f"Article #{post['id']}: {post['title']} | user: {args.user}")
        print(f"SIMULATED history: {plan['start']} through {plan['end']}; today's real metrics are unchanged.")
        rank, traffic = plan['rank'], plan['traffic']
        print(f"Rank: #{rank['prior_position']} → #{rank['recent_position']}; "
              f"visits: {traffic['sessions_prev_7d']} → {traffic['sessions_7d']}; "
              f"affiliate clicks (recent 7 days): {traffic['affiliate_clicks_7d']}")
        print(f"Recommendation: {plan['decision']['decision']} — {plan['decision']['reason']}")
        print(f"New rows: {len(plan['rankings'])} rankings, {len(plan['clicks'])} traffic days.")
        if args.apply:
            apply_plan(conn, args.user, plan)
    if args.apply:
        print('Applied. Refresh the dashboard on Vercel. No article content was changed.')
    else:
        print('Dry run only. Add --apply to insert this history.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True, help='Exact account username owning the article')
    parser.add_argument('--post', type=int, help='Published article ID (find it with --list)')
    parser.add_argument('--scenario', choices=SCENARIOS, default='rewrite')
    parser.add_argument('--list', action='store_true', help='List only this user’s published article IDs')
    parser.add_argument('--apply', action='store_true', help='Write after validation; otherwise preview only')
    args = parser.parse_args()
    if args.list and (args.post is not None or args.apply):
        parser.error('--list cannot be combined with --post or --apply')
    if not args.list and (args.post is None or args.post < 1):
        parser.error('Supply a positive --post ID, or use --list')
    if not settings.database_url:
        parser.exit(1, 'DATABASE_URL is required; no in-memory seeding is performed.\n')
    import psycopg
    from psycopg.rows import dict_row
    try:
        with psycopg.connect(settings.database_url, connect_timeout=15, prepare_threshold=None,
                             row_factory=dict_row) as conn:
            run(conn, args)
    except ValueError as exc:
        parser.exit(1, f'{exc}\n')
    except psycopg.Error:
        # Connection errors may include credentials or endpoint details; do not print the exception.
        parser.exit(1, 'Database operation failed and was rolled back. Check connectivity/configuration and retry the dry run.\n')


if __name__ == '__main__':
    main()
