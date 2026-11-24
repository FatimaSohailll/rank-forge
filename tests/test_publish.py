"""Publishing behind human approval: gate, approvals, publisher, public pages, tracked links."""
import re

import pytest
from fastapi.testclient import TestClient

from agents import publish
from stores import approvals, drafts, posts

SUPERVISOR = "You are RankForge, an AI SEO"
REI = {"id": 7, "name": "REI Co-op", "network": "AvantLink", "commission_pct": 5.0,
       "base_url": "https://www.rei.com/search?q={q}"}

ARTICLE = (
    "# Best Budget Hiking Daypacks\n\n"
    "A good [daypack](https://www.rei.com/search?q=daypack) makes hikes easier. See the "
    "[trail guide](https://example.com/guide) too. " + "Short sentences read well. " * 20 + "\n\n"
    "## What to look for\n\nComfort first. Another [pack](https://www.rei.com/search?q=pack) option.\n\n"
    "*Disclosure: this article contains affiliate links.*"
)


def save_final(user="alice", body=ARTICLE, score=95):
    return drafts.save_draft(user, None, "final", body, seo_report={"score": score, "stats": {"words": 1200}})


@pytest.fixture
def client(fake_llm):
    import main

    def route(system, messages):
        text = messages[-1]["content"].lower()
        kind = "publish" if "publish" in text else "chat"
        return {"route": kind, "reply": "ok", "topic": "", "location": ""}
    fake_llm.on(SUPERVISOR, route)
    with TestClient(main.app) as c:
        yield c


def chat(client, user, message, thread="t1"):
    r = client.post("/chat", json={"user_id": user, "thread_id": thread, "message": message})
    assert r.status_code == 200
    return r.json()["reply"]


def supervisor_calls(fake_llm):
    return sum(1 for c in fake_llm.calls if SUPERVISOR in c["system"])


# ---------- pure helpers ----------

def test_slugify_and_unique_slug():
    assert publish.slugify("Best Budget Hiking Daypacks: 2026!") == "best-budget-hiking-daypacks-2026"
    assert publish.slugify("???") == "post"
    posts.create_post("alice", "my-post", "t", "k", "", "<p>x</p>", {})
    assert publish.unique_slug("my-post") == "my-post-2"
    assert publish.unique_slug("fresh") == "fresh"


def test_split_title_removes_only_the_first_h1():
    title, body = publish.split_title("# Title\n\nBody\n\n## Section", "fallback")
    assert title == "Title" and body.startswith("Body") and "## Section" in body
    assert publish.split_title("no heading here", "fallback") == ("fallback", "no heading here")


def test_track_links_only_rewrites_affiliate_hosts():
    md, tracked = publish.track_links(ARTICLE, [REI])
    assert len(tracked) == 2 and all(t["program_id"] == 7 for t in tracked)
    assert "rei.com" not in md and "https://example.com/guide" in md
    assert all(f"(/r/{t['code']})" in md for t in tracked)
    assert len({t["code"] for t in tracked}) == 2


def test_render_html_escapes_raw_html_and_blocks_javascript_links():
    html = publish.render_html("Hi <script>alert(1)</script>\n\n[x](javascript:alert(1)) and [ok](https://example.com)")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert 'rel="noopener" target="_blank"' in html


def test_render_html_marks_tracked_links_as_sponsored():
    html = publish.render_html("[daypack](/r/abc123)")
    assert 'href="/r/abc123" rel="sponsored nofollow noopener"' in html


def test_meta_description_prefers_the_brief_then_first_real_paragraph():
    assert publish.meta_description("x", "  From the brief.  ") == "From the brief."
    text = "# T\n\nShort.\n\nThis paragraph has plenty of words to be a decent description of the page."
    assert publish.meta_description(text).startswith("This paragraph has plenty")


# ---------- approvals store ----------

def test_approval_resolves_exactly_once_and_only_for_its_owner():
    a = approvals.create("alice", "alice:t1", "risky_action", {"draft_id": 1})
    assert approvals.pending_for_thread("alice", "alice:t1")["resume_token"] == a["resume_token"]
    assert approvals.pending_for_thread("bob", "alice:t1") is None
    assert approvals.resolve("bob", a["resume_token"], "approved") is None        # not the owner
    assert approvals.resolve("alice", "wrong-token", "approved") is None
    assert approvals.resolve("alice", a["resume_token"], "approved")["status"] == "approved"
    assert approvals.resolve("alice", a["resume_token"], "approved") is None      # already used
    assert approvals.list_pending("alice") == []
    with pytest.raises(ValueError):
        approvals.resolve("alice", a["resume_token"], "pending")


# ---------- the publish flow in chat ----------

def test_publish_without_a_finished_article(client, fake_llm):
    assert "no finished article" in chat(client, "alice", "publish it")
    assert approvals.list_pending("alice") == [] and posts.list_published() == []


def test_publish_request_pauses_and_nothing_goes_live(client):
    save_final()
    reply = chat(client, "alice", "publish it")
    assert "Nothing is public yet" in reply and "approve" in reply and "Best Budget Hiking Daypacks" in reply
    assert posts.list_published() == []
    assert drafts.latest_draft("alice", "final") is not None                     # still just a draft
    assert len(approvals.list_pending("alice")) == 1


def test_other_messages_while_pending_get_a_reminder_not_the_model(client, fake_llm):
    save_final()
    chat(client, "alice", "publish it")
    before = supervisor_calls(fake_llm)
    assert "waiting" in chat(client, "alice", "also research some keywords")
    assert "waiting" in chat(client, "alice", "publish it")                      # repeating the request is NOT an approval
    assert supervisor_calls(fake_llm) == before and posts.list_published() == []


def test_approve_publishes_with_tracked_links_and_no_model_call(client, fake_llm):
    save_final()
    chat(client, "alice", "publish it")
    before = len(fake_llm.calls)
    reply = chat(client, "alice", "approve")
    assert reply.startswith("Published:") and "/posts/best-budget-hiking-daypacks" in reply
    assert len(fake_llm.calls) == before                                          # approval is deterministic
    post = posts.get_published("best-budget-hiking-daypacks")
    assert post["user_id"] == "alice" and post["title"] == "Best Budget Hiking Daypacks"
    assert "rei.com" not in post["html"] and len(re.findall(r'href="/r/[\w-]+"', post["html"])) == 2
    assert 'href="https://example.com/guide"' in post["html"]
    assert post["schema_jsonld"]["@type"] == "Article"
    assert drafts.latest_draft("alice", "final") is None                         # marked published
    assert approvals.list_pending("alice") == []


def test_reject_publishes_nothing_and_a_later_request_works(client):
    save_final()
    chat(client, "alice", "publish it")
    assert "won't publish" in chat(client, "alice", "reject")
    assert posts.list_published() == [] and drafts.latest_draft("alice", "final") is not None
    assert "Nothing is public yet" in chat(client, "alice", "publish it")        # asks again
    assert "Published:" in chat(client, "alice", "yes")


def test_a_published_draft_cannot_be_published_twice(client):
    save_final()
    chat(client, "alice", "publish it")
    chat(client, "alice", "approve")
    assert "no finished article" in chat(client, "alice", "publish it")
    assert len(posts.list_published()) == 1


def test_approve_with_nothing_pending_gets_a_fixed_answer_not_the_model(client, fake_llm):
    before = len(fake_llm.calls)
    assert chat(client, "alice", "approve") == "There's nothing waiting for your approval right now."
    assert chat(client, "alice", "Rejected!") == "There's nothing waiting for your approval right now."
    assert len(fake_llm.calls) == before and posts.list_published() == []
    assert "waiting for your approval" not in chat(client, "alice", "yes")      # "yes" stays ordinary chat


def test_another_user_cannot_approve_or_see_alices_request(client):
    save_final()
    chat(client, "alice", "publish it")
    assert chat(client, "bob", "approve") == "There's nothing waiting for your approval right now."
    assert posts.list_published() == []
    assert client.get("/approvals", params={"user_id": "bob"}).json() == {"approvals": []}


def test_approval_is_bound_to_the_draft_the_human_was_shown(client):
    save_final(body=ARTICLE.replace("Best Budget Hiking Daypacks", "The Draft I Approved"))
    chat(client, "alice", "publish it")
    save_final(body=ARTICLE.replace("Best Budget Hiking Daypacks", "A Newer Draft Nobody Approved"))
    assert "Published:" in chat(client, "alice", "approve")
    titles = [p["title"] for p in posts.list_published()]
    assert titles == ["The Draft I Approved"]


def test_published_html_is_safe_even_if_the_article_contains_scripts(client):
    save_final(body="# Safe Title\n\nHello <script>alert(1)</script> world [bad](javascript:alert(1)) "
                    + "words here " * 10)
    chat(client, "alice", "publish it")
    chat(client, "alice", "approve")
    page = client.get("/posts/safe-title")
    assert page.status_code == 200 and "<script>alert" not in page.text.split("</head>")[1]
    assert 'href="javascript:' not in page.text


# ---------- /resume and /approvals ----------

def pending_token(client, user="alice"):
    return client.get("/approvals", params={"user_id": user}).json()["approvals"][0]["resume_token"]


def resume(client, user, token, decision, **extra):
    r = client.post("/resume", json={"user_id": user, "resume_token": token, "decision": decision, **extra})
    assert r.status_code == 200
    return r.json()


def test_resume_endpoint_approves_exactly_once(client):
    save_final()
    chat(client, "alice", "publish it")
    token = pending_token(client)
    first = resume(client, "alice", token, "approve")
    assert first["reply"].startswith("Published:") and set(first) == {"reply", "tool_calls", "tokens"}
    assert "couldn't find a pending approval" in resume(client, "alice", token, "approve")["reply"]
    assert len(posts.list_published()) == 1


def test_resume_with_someone_elses_token_does_nothing(client):
    save_final()
    chat(client, "alice", "publish it")
    token = pending_token(client)
    assert "couldn't find" in resume(client, "bob", token, "approve")["reply"]
    assert posts.list_published() == [] and len(approvals.list_pending("alice")) == 1


def test_resume_reject_and_unknown_token(client):
    save_final()
    chat(client, "alice", "publish it")
    assert "won't publish" in resume(client, "alice", pending_token(client), "reject")["reply"]
    assert "couldn't find" in resume(client, "alice", "no-such-token-123", "approve")["reply"]
    assert posts.list_published() == []


def test_resume_amend_changes_title_and_slug(client):
    save_final()
    chat(client, "alice", "publish it")
    out = resume(client, "alice", pending_token(client), "amend", title="My Better Title", slug="Better Slug!")
    assert "/posts/better-slug" in out["reply"]
    assert posts.get_published("better-slug")["title"] == "My Better Title"


def test_resume_validates_its_input(client):
    r = client.post("/resume", json={"user_id": "alice", "resume_token": "x", "decision": "maybe"})
    assert r.status_code == 422


def test_streaming_publish_request_ends_with_the_approval_prompt(client):
    save_final()
    r = client.post("/chat/stream", json={"user_id": "alice", "thread_id": "s1", "message": "publish it"})
    assert "Nothing is public yet" in r.text and "event: done" in r.text
    assert posts.list_published() == []


# ---------- public pages and click tracking ----------

@pytest.fixture
def published(client):
    save_final()
    chat(client, "alice", "publish it")
    chat(client, "alice", "approve")
    return posts.get_published("best-budget-hiking-daypacks")


def test_article_page_has_seo_markup_security_headers_and_counts_views(client, published):
    r = client.get("/posts/best-budget-hiking-daypacks")
    assert r.status_code == 200
    assert "<title>Best Budget Hiking Daypacks</title>" in r.text
    assert 'rel="canonical"' in r.text and 'application/ld+json' in r.text and '"@type": "Article"' in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    client.get("/posts/best-budget-hiking-daypacks")
    assert posts.today_counts(published["id"])["sessions"] == 2


def test_unknown_or_malformed_slugs_are_404(client, published):
    assert client.get("/posts/does-not-exist").status_code == 404
    assert client.get("/posts/Bad_Slug").status_code == 404


def test_tracked_link_redirects_and_counts_the_click(client, published):
    code = re.search(r'href="/r/([\w-]+)"', published["html"]).group(1)
    r = client.get(f"/r/{code}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://www.rei.com/search?q=")
    assert "noindex" in r.headers["x-robots-tag"]
    client.get(f"/r/{code}", follow_redirects=False)
    assert posts.today_counts(published["id"])["affiliate_clicks"] == 2


def test_unknown_redirect_codes_are_404_and_never_open_redirects(client, published):
    assert client.get("/r/nope1234", follow_redirects=False).status_code == 404
    assert client.get("/r/..%2f..%2fetc", follow_redirects=False).status_code == 404


def test_sitemap_and_robots(client, published):
    sm = client.get("/sitemap.xml")
    assert sm.status_code == 200 and "/posts/best-budget-hiking-daypacks" in sm.text
    robots = client.get("/robots.txt").text
    assert "Disallow: /r/" in robots and "Sitemap:" in robots
