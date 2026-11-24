"""Deterministic scenarios plus transaction and isolation checks without shared DB writes."""
from contextlib import contextmanager
from datetime import date, timedelta
import importlib.util
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agents import optimizer, rank_tracker

SPEC = importlib.util.spec_from_file_location('simulate_performance', Path(__file__).resolve().parents[1] / 'scripts/simulate_performance.py')
sim = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sim)
TODAY = date(2026, 11, 24)
POST = dict(id=1, user_id='alice', title='Hiking guide', keyword='hiking gear', status='published', rewrite_count=0)


@pytest.mark.parametrize('scenario', sim.SCENARIOS)
def test_scenarios_use_real_summaries_and_decisions(scenario):
    plan = sim.plan_simulation(POST, [], [], scenario, TODAY)
    assert plan['decision']['decision'] == scenario
    assert len(plan['rankings']) == len(plan['clicks']) == 28
    assert all(TODAY - timedelta(days=28) <= r['date'] < TODAY for r in plan['rankings'])
    assert all(r['source'] == 'simulated' for r in plan['rankings'])
    assert all(r['conversions'] == 0 and r['affiliate_clicks'] <= r['sessions'] for r in plan['clicks'])
    repeated = sim.plan_simulation(POST, plan['rankings'], plan['clicks'], scenario, TODAY)
    assert repeated['rankings'] == repeated['clicks'] == []


def test_conflicts_and_real_traffic_with_matching_counts_are_refused():
    plan = sim.plan_simulation(POST, [], [], today=TODAY)
    rank = plan['rankings'][0]
    with pytest.raises(ValueError, match='Conflicting'):
        sim.plan_simulation(POST, [{**rank, 'source': 'tavily'}], [], today=TODAY)
    with pytest.raises(ValueError, match='Conflicting'):
        sim.plan_simulation(POST, [{**rank, 'keyword': 'other'}], [], today=TODAY)
    with pytest.raises(ValueError, match='no matching simulated ranking'):
        sim.plan_simulation(POST, [], plan['clicks'][:1], today=TODAY)


def test_loop_limit_and_combined_traffic_are_checked_before_writing():
    with pytest.raises(ValueError, match='requested scenario'):
        sim.plan_simulation({**POST, 'rewrite_count': 100}, [], [], today=TODAY)
    today_click = dict(post_id=1, date=TODAY, sessions=10000, affiliate_clicks=10, conversions=0)
    with pytest.raises(ValueError, match='requested scenario'):
        sim.plan_simulation(POST, [], [today_click], 'retarget', TODAY)


def test_mixed_real_and_demo_history_keeps_clear_labels():
    plan = sim.plan_simulation(POST, [], [], today=TODAY)
    ranks = plan['rankings'] + [dict(post_id=1, keyword='hiking gear', date=TODAY, position=20, source='tavily')]
    summary = rank_tracker.summarise(sorted(ranks, key=lambda r: r['date']), TODAY)[0]
    assert summary['source'] == 'tavily' and summary['has_simulated']
    report = optimizer._report([dict(title='Hiking', rank=summary, clicks={}, decision='rewrite',
                                      reason='slipped', escalate=False)])
    assert 'simulated rankings, visits, and affiliate clicks' in report


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def fetchall(self):
        rows = [dict(r) for r in self.cursor.fetchall()]
        for row in rows:
            if 'date' in row:
                row['date'] = date.fromisoformat(row['date'])
        return rows

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None


class LocalDatabase:
    """SQLite adapter exercises insert-only SQL and real transaction rollback offline."""
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            create table posts (id integer primary key, user_id text, title text, keyword text,
                                status text, rewrite_count integer);
            create table rankings (user_id text, post_id integer, keyword text, date text,
                                   position integer, source text, unique(post_id, keyword, date));
            create table clicks (user_id text, post_id integer, date text, sessions integer,
                                 affiliate_clicks integer, conversions integer, unique(post_id, date));
        ''')
        self.db.execute('insert into posts values (1, "alice", "Hiking", "hiking gear", "published", 0)')
        self.db.execute('insert into posts values (2, "bob", "Coffee", "coffee", "published", 0)')
        self.db.commit()

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def execute(self, sql, params=()):
        params = tuple(v.isoformat() if isinstance(v, date) else v for v in params)
        return Cursor(self.db.execute(sql.replace('%s', '?').replace(' for update', ''), params))


@pytest.fixture
def local_db():
    db = LocalDatabase()
    yield db
    db.db.close()


def args(**kwargs):
    return SimpleNamespace(**(dict(user='alice', post=1, scenario='rewrite', apply=False, list=False) | kwargs))


def count_rows(db):
    return tuple(db.db.execute(f'select count(*) from {table}').fetchone()[0] for table in ('rankings', 'clicks'))


def test_dry_run_apply_rerun_and_isolation(local_db, capsys):
    today = date.today()
    local_db.execute('insert into clicks values (%s,%s,%s,%s,%s,%s)', ('alice', 1, today, 3, 1, 0))
    local_db.execute('insert into clicks values (%s,%s,%s,%s,%s,%s)', ('bob', 2, today - timedelta(days=1), 500, 50, 0))
    local_db.db.commit()
    before = local_db.execute('select * from clicks order by post_id').fetchall()
    sim.run(local_db, args())
    assert count_rows(local_db) == (0, 2)
    assert 'Dry run only' in capsys.readouterr().out
    sim.run(local_db, args(apply=True))
    assert count_rows(local_db) == (28, 30)
    sim.run(local_db, args(apply=True))
    assert count_rows(local_db) == (28, 30)
    assert local_db.execute('select * from clicks where date=%s or post_id=2 order by post_id', (today,)).fetchall() == before
    with pytest.raises(ValueError, match='belonging'):
        sim.run(local_db, args(user='bob', apply=True))
    assert count_rows(local_db) == (28, 30)


def test_conflicts_and_mid_insert_failure_are_atomic(local_db):
    yesterday = date.today() - timedelta(days=1)
    local_db.execute('insert into clicks values (%s,%s,%s,%s,%s,%s)', ('alice', 1, yesterday, 999, 99, 0))
    local_db.db.commit()
    with pytest.raises(ValueError, match='Conflicting'):
        sim.run(local_db, args(apply=True))
    assert count_rows(local_db) == (0, 1)
    local_db.db.execute('delete from clicks')
    local_db.db.commit()
    local_db.db.executescript("create trigger reject_click before insert on clicks begin select raise(abort, 'test failure'); end;")
    with pytest.raises(sqlite3.IntegrityError):
        sim.run(local_db, args(apply=True))
    assert count_rows(local_db) == (0, 0)


def test_list_and_unpublished_post(local_db, capsys):
    sim.run(local_db, args(list=True))
    output = capsys.readouterr().out
    assert 'Hiking' in output and 'Coffee' not in output
    local_db.db.execute('update posts set status="archived" where id=1')
    local_db.db.commit()
    with pytest.raises(ValueError, match='published'):
        sim.run(local_db, args(apply=True))
    assert count_rows(local_db) == (0, 0)
