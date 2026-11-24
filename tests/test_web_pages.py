"""Website: landing, signup/login, customer pages, admin dashboard, CSRF, isolation."""
import pytest
from fastapi.testclient import TestClient

from middleware import auth
from stores import approvals, drafts, posts, users

SUPERVISOR = "You are RankForge, an AI SEO"
ARTICLE = ("# Best Budget Hiking Daypacks\n\nA good [daypack](https://www.rei.com/search?q=daypack) helps. "
           + "Short sentences read well. " * 30 + "\n\n<script>alert(1)</script>")


@pytest.fixture
def client(fake_llm):
    import main
    fake_llm.on(SUPERVISOR, lambda s, m: {"route": "publish" if "publish" in m[-1]["content"].lower() else "chat",
                                          "reply": "ok", "topic": "", "location": ""})
    with TestClient(main.app) as c:
        yield c


def login(client, user="alice", password="longenough", admin=False):
    if not users.get(user):
        auth.signup(user, password)
    if admin:
        users._mem[user]["is_admin"] = True
    r = client.post("/login", data={"username": user, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return r


def test_landing_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "RankForge" in r.text and "/signup" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api").json()["name"] == "RankForge Agent API"


def test_app_pages_need_login(client):
    for path in ("/app", "/app/generator", "/app/articles", "/app/articles/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


def test_signup_logs_in_and_sets_a_safe_cookie(client):
    r = client.post("/signup", data={"username": "newbie", "password": "longenough"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/app").status_code == 200
    bad = client.post("/signup", data={"username": "x", "password": "longenough"})
    assert bad.status_code == 400 and "Username" in bad.text


def test_login_failures_and_open_redirects(client):
    auth.signup("alice", "longenough")
    assert client.post("/login", data={"username": "alice", "password": "nope"}).status_code == 401
    r = client.post("/login", data={"username": "alice", "password": "longenough", "next": "//evil.com"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app"


def test_logout(client):
    login(client)
    client.post("/logout")
    assert client.get("/app", follow_redirects=False).status_code == 303


def test_cross_site_form_posts_are_refused(client):
    auth.signup("alice", "longenough")
    r = client.post("/login", data={"username": "alice", "password": "longenough"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_dashboard_generator_and_articles_render(client):
    login(client)
    drafts.save_draft("alice", None, "final", ARTICLE, seo_report={"score": 91, "stats": {"words": 300}})
    posts.create_post("alice", "alice-post", "Alice Post", "kw", "", "<p>x</p>", {})
    dash = client.get("/app")
    assert dash.status_code == 200 and "Optimizer recommendations" in dash.text and "Alice Post" in dash.text
    gen = client.get("/app/generator")
    assert gen.status_code == 200 and 'id="chat-form"' in gen.text
    arts = client.get("/app/articles")
    assert arts.status_code == 200 and "Alice Post" in arts.text and "SEO" in arts.text


def test_workspace_is_owner_only_and_renders_safely(client):
    did = drafts.save_draft("bob", None, "final", ARTICLE, seo_report={"score": 91, "stats": {"words": 300}})
    login(client)
    assert client.get(f"/app/articles/{did}").status_code == 404                 # bob's draft
    mine = drafts.save_draft("alice", None, "final", ARTICLE, seo_report={"score": 91, "issues": ["x"],
                                                                         "passed": [], "stats": {"words": 300}})
    page = client.get(f"/app/articles/{mine}")
    assert page.status_code == 200 and "Best Budget Hiking Daypacks" in page.text
    assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text


def test_publish_button_flow_for_a_chosen_draft(client, fake_llm):
    login(client)
    older = drafts.save_draft("alice", None, "final", "# Older Draft\n\n" + "Words here. " * 40)
    drafts.save_draft("alice", None, "final", "# Newer Draft\n\n" + "Words here. " * 40)
    r = client.post("/chat", json={"user_id": "alice", "thread_id": f"workspace-{older}",
                                   "message": "publish this draft", "action": "publish", "target_id": older})
    assert "Older Draft" in r.json()["reply"] and not any(SUPERVISOR in c["system"] for c in fake_llm.calls)
    pending = client.get("/approvals").json()["approvals"]
    assert pending[0]["action"]["draft_id"] == older and pending[0]["thread_key"] == f"alice:workspace-{older}"
    done = client.post("/resume", json={"user_id": "alice", "resume_token": pending[0]["resume_token"],
                                        "decision": "approve"}, headers={"X-RankForge-UI": "1"})
    assert done.json()["reply"].startswith("Published:") and "reply_html" in done.json()
    assert posts.get_published("older-draft")


def test_publish_button_cannot_publish_someone_elses_draft(client):
    bobs = drafts.save_draft("bob", None, "final", "# Bob Draft\n\n" + "Words. " * 40)
    login(client)
    r = client.post("/chat", json={"user_id": "alice", "thread_id": "w", "message": "publish",
                                   "action": "publish", "target_id": bobs})
    assert "no finished article" in r.json()["reply"]
    assert approvals.list_pending("alice") == [] and approvals.list_pending("bob") == []


def test_unpublish_and_restore_own_post_only(client):
    login(client)
    mine = posts.create_post("alice", "alice-p", "Mine", "k", "", "<p>x</p>", {})
    bobs = posts.create_post("bob", "bob-p", "Bob's", "k", "", "<p>x</p>", {})
    assert client.post(f"/app/posts/{bobs['id']}/status", data={"status": "archived"}).status_code == 404
    client.post(f"/app/posts/{mine['id']}/status", data={"status": "archived"})
    assert client.get("/posts/alice-p").status_code == 404 and client.get("/posts/bob-p").status_code == 200
    client.post(f"/app/posts/{mine['id']}/status", data={"status": "published"})
    assert client.get("/posts/alice-p").status_code == 200


def test_stream_returns_rendered_html_for_the_ui(client):
    login(client)
    r = client.post("/chat/stream", json={"user_id": "alice", "thread_id": "t", "message": "hi"})
    assert "event: done" in r.text and "reply_html" in r.text


def test_admin_pages_are_admin_only(client):
    login(client)
    for path in ("/admin", "/admin/users", "/admin/content"):
        assert client.get(path).status_code == 403


def test_admin_can_see_everything_and_moderate(client, app_graph):
    posts.create_post("bob", "bob-p", "Bob's Post", "k", "", "<p>x</p>", {})
    users.ensure("bob")
    app_graph.run_turn("bob", "t1", "hello")
    login(client, "boss", admin=True)
    analytics = client.get("/admin")
    assert analytics.status_code == 200 and "supervisor" in analytics.text and "Spend today" in analytics.text
    user_page = client.get("/admin/users")
    assert "bob" in user_page.text and "API only" in user_page.text
    content = client.get("/admin/content")
    assert "Bob&#39;s Post" in content.text or "Bob's Post" in content.text
    post_id = posts.list_all()[0]["id"]
    client.post(f"/admin/posts/{post_id}/status", data={"status": "archived"})
    assert client.get("/posts/bob-p").status_code == 404
    client.post("/admin/users/bob/disabled", data={"disabled": "1"})
    assert users.get("bob")["disabled"]


def test_bootstrap_admin(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "admin_password", "admin-pass-123")
    auth.bootstrap_admin()
    assert auth.login("admin", "admin-pass-123")["is_admin"]


def test_generator_recovers_owned_final_draft_after_refresh(client):
    login(client)
    mine = drafts.save_draft('alice', None, 'final', '# My saved article\n\n<script>bad()</script>',
                             seo_report={'score': 88, 'stats': {'words': 200}})
    drafts.save_draft('bob', None, 'final', '# Private article\n\nPrivate text')
    drafts.save_draft('alice', None, 'draft', '# Unfinished article\n\nPartial text')
    page = client.get('/app/generator')
    assert 'id="saved-draft"' in page.text
    assert f'data-draft="{mine}"' in page.text
    assert 'Review and publish your saved article' in page.text
    assert 'Private article' not in page.text and 'Unfinished article' not in page.text
    assert '<script>bad()</script>' not in page.text and '&lt;script&gt;' in page.text
    # Starting a different topic should not offer an unrelated article.
    assert 'id="saved-draft"' not in client.get('/app/generator?topic=camping').text
    drafts.set_stage('alice', mine, 'published')
    assert 'id="saved-draft"' not in client.get('/app/generator').text


def test_dashboard_labels_simulated_history_with_latest_real_ranking(client):
    from datetime import date, timedelta
    from stores import metrics
    login(client)
    post = posts.create_post('alice', 'mixed-demo', 'Mixed demo article', 'hiking', '', '<p>Guide</p>', {})
    metrics.add_ranking('alice', post['id'], 'hiking', 22, 'simulated', date.today() - timedelta(days=1))
    metrics.add_ranking('alice', post['id'], 'hiking', 20, 'tavily', date.today())
    metrics.add_clicks('alice', post['id'], date.today() - timedelta(days=1), 50, 4)
    page = client.get('/app')
    assert page.status_code == 200
    assert 'includes simulated rankings, visits, and affiliate clicks' in page.text
    assert '<span class="tag">simulated</span>' in page.text
