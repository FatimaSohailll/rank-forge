"""Draft revision, streamed result metadata, and version-bound approvals."""
import pytest

from agents.link_builder import DISCLOSURE
from middleware.cost_guard import CostCeilingHit
from stores import approvals, briefs, drafts, posts

ARTICLE = '# Hiking Gear\n\nA [daypack](https://www.rei.com/search?q=daypack) helps on a walk.\n\n' + DISCLOSURE
REVISED = ARTICLE.replace('helps on a walk.', 'keeps your gear together. Pack lightly and enjoy the trail.')


@pytest.fixture
def source(fake_llm, monkeypatch):
    from tools import web
    from tools.seo_audit import seo_audit
    monkeypatch.setattr(web, 'web_search', lambda *a, **k: [])
    fake_llm.on('article revision editor', REVISED)
    fake_llm.on('extract checkable', {'claims': []})
    fake_llm.on('You are the Editor', lambda s, m: REVISED)
    bid = briefs.save_brief('alice', 'hiking gear', {'title': 'Hiking Gear', 'word_count_target': 1000,
                                                  'affiliate_programs': []})
    return drafts.save_draft('alice', bid, 'final', ARTICLE, seo_report=seo_audit(ARTICLE, 'hiking gear'))


def revise(app_graph, did, user='alice', message='Make the introduction more friendly'):
    return list(app_graph.stream_turn(user, 'edit', message, {'type': 'revise', 'draft_id': did}))


def test_revision_preserves_source_and_runs_qa(app_graph, source, fake_llm):
    # Newer unrelated content must never become the revision target.
    drafts.save_draft('alice', None, 'final', '# Unrelated\n\nOther article')
    events = revise(app_graph, source)
    done = events[-1][1]
    article = done['article']
    assert article['draft_id'] != source
    assert article['workspace_url'] == f"/app/articles/{article['draft_id']}"
    assert article['title'] == 'Hiking Gear'
    assert drafts.get_draft('alice', source)['body'] == ARTICLE
    assert drafts.get_draft('alice', article['draft_id'])['body'] == REVISED
    assert not posts.list_for_user('alice')
    nodes = [data['node'] for event, data in events if event == 'progress' and data['status'] == 'complete']
    assert all(n in nodes for n in ('reviser', 'fact_checker', 'link_builder', 'editor'))
    for node in ('reviser', 'fact_checker', 'link_builder', 'editor'):
        statuses = [d['status'] for e, d in events if e == 'progress' and d['node'] == node]
        assert statuses == ['started', 'complete']


@pytest.mark.parametrize('owner', ['bob', 'alice'])
def test_revision_rejects_foreign_or_published_draft(app_graph, source, owner, fake_llm):
    if owner == 'alice':
        drafts.set_stage('alice', source, 'published')
    done = revise(app_graph, source, user=owner)[-1][1]
    assert 'article' not in done
    assert not any('article revision editor' in c['system'] for c in fake_llm.calls)


@pytest.mark.parametrize('failure', ['refusal', 'empty', 'missing_link', 'cost'])
def test_unsuccessful_revision_never_offers_stale_article(app_graph, source, fake_llm, failure):
    def fail(s, m):
        import llm
        if failure == 'refusal':
            raise llm.LLMRefusal('refused')
        if failure == 'cost':
            raise CostCeilingHit('limit')
        return '' if failure == 'empty' else '# Hiking Gear\n\nMissing the link'
    fake_llm.handlers.insert(0, ('article revision editor', fail))
    events = revise(app_graph, source)
    assert 'article' not in events[-1][1]
    assert drafts.get_draft('alice', source)['body'] == ARTICLE
    assert not any(e == 'progress' and d['node'] == 'editor' for e, d in events)


def test_pending_publish_must_be_cancelled_before_revision(app_graph, source, fake_llm):
    action = {'type': 'publish', 'draft_id': source}
    app_graph.run_turn('alice', 'edit', 'publish this draft', action)
    approval = approvals.pending_for_thread('alice', 'alice:edit')
    assert approval['action']['draft_id'] == source
    assert 'article' not in revise(app_graph, source)[-1][1]
    assert not any('article revision editor' in c['system'] for c in fake_llm.calls)
    app_graph.resume_turn('alice', approval['resume_token'], 'reject')
    revised = revise(app_graph, source)[-1][1]['article']['draft_id']
    assert not posts.list_for_user('alice')
    app_graph.run_turn('alice', 'edit', 'publish revised article', {'type': 'publish', 'draft_id': revised})
    approval = approvals.pending_for_thread('alice', 'alice:edit')
    assert approval['action']['draft_id'] == revised
    app_graph.resume_turn('alice', approval['resume_token'], 'approve')
    assert len(posts.list_for_user('alice')) == 1
    app_graph.resume_turn('alice', approval['resume_token'], 'approve')
    assert len(posts.list_for_user('alice')) == 1


def test_later_chat_has_no_stale_ready_metadata(app_graph, source):
    assert 'article' in revise(app_graph, source)[-1][1]
    events = list(app_graph.stream_turn('alice', 'edit', 'thanks'))
    assert 'article' not in events[-1][1]


def test_revision_of_rewrite_keeps_live_url_until_confirmed(app_graph, fake_llm, source):
    live = posts.create_post('alice', 'hiking-original', 'Original', 'hiking gear', '', '<p>Original content</p>', {})
    bid = briefs.save_brief('alice', 'hiking gear', {'title': 'Hiking Gear', 'rewrite_of': live['id'],
                                                  'affiliate_programs': [], 'word_count_target': 1000})
    did = drafts.save_draft('alice', bid, 'final', ARTICLE)
    revised = revise(app_graph, did)[-1][1]['article']['draft_id']
    assert posts.get_for_user('alice', live['id'])['html'] == '<p>Original content</p>'
    app_graph.run_turn('alice', 'edit', 'publish revised draft', {'type': 'publish', 'draft_id': revised})
    approval = approvals.pending_for_thread('alice', 'alice:edit')
    assert approval['action']['replaces_post_id'] == live['id']
    assert posts.get_for_user('alice', live['id'])['html'] == '<p>Original content</p>'
    result = app_graph.resume_turn('alice', approval['resume_token'], 'approve')
    assert '[Read your article]' in result['reply']
    updated = posts.get_for_user('alice', live['id'])
    assert updated['slug'] == 'hiking-original'
    assert 'keeps your gear together' in updated['html']
    assert len(posts.list_for_user('alice')) == 1


def test_revision_request_requires_target_and_session_user_wins(fake_llm):
    from fastapi.testclient import TestClient
    import main
    from middleware import auth
    with TestClient(main.app) as client:
        missing = {'user_id': 'alice', 'thread_id': 'edit', 'message': 'Shorten the introduction', 'action': 'revise'}
        assert client.post('/chat/stream', json=missing).status_code == 422
        invalid = client.post('/chat', json=missing)
        assert invalid.status_code == 200 and 'article' not in invalid.json()
        auth.signup('alice', 'longenough')
        client.post('/login', data={'username': 'alice', 'password': 'longenough'})
        bid = briefs.save_brief('bob', 'hiking', {'title': 'Bob draft'})
        did = drafts.save_draft('bob', bid, 'final', ARTICLE)
        body = {**missing, 'user_id': 'bob', 'target_id': did}
        from test_pipeline import parse_sse
        events = parse_sse(client.post('/chat/stream', json=body).text)
        assert 'article' not in events[-1][1]
        assert not any('article revision editor' in c['system'] for c in fake_llm.calls)
