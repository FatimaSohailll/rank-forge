"""RankForge agent API.

POST /chat        {"user_id", "thread_id", "message"} -> {"reply", "tool_calls", "tokens": {"in", "out"}}
POST /chat/stream same request, Server-Sent Events: `progress` per finished step, then `done` (same body as /chat)
POST /resume      approve / reject / amend a pending publish request by resume_token
GET  /approvals   pending approvals for a user
GET  /posts/{slug}, /r/{code}, /sitemap.xml, /robots.txt   public pages and tracked affiliate redirects
GET  /, /login, /signup, /app/*, /admin/*                 website (landing, customer app, admin dashboard)
POST /cron/run    Rank Tracker + Optimizer (needs X-Cron-Secret)
GET  /health -> ok

Who the API acts as: a logged-in session wins; otherwise the body's user_id, but only for API-only users
(login accounts and admins always need their session). See middleware/auth.py.
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from typing import Literal

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from config import settings
import graph
from agents.publish import render_html
from middleware import auth, rate_limit
from stores import approvals
from web.admin import router as admin_router
from web.app_pages import router as app_router
from web.auth_pages import router as auth_router
from web.common import LoginRedirect, render
from web.cron import router as cron_router
from web.public import router as public_router

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rankforge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    graph.init()
    auth.bootstrap_admin()
    yield
    graph.shutdown()


app = FastAPI(title="RankForge Agent API", version="0.1.0", lifespan=lifespan)

# ---------- HTTP middleware ----------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def request_timing(request: Request, call_next):
    """Tag every request with an id and log its latency as one JSON line."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    ms = int((time.perf_counter() - start) * 1000)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-ms"] = str(ms)
    log.info(json.dumps({"event": "http", "id": request_id, "method": request.method,
                         "path": request.url.path, "status": response.status_code, "ms": ms}))
    return response


def chat_error(reply: str) -> JSONResponse:
    """Errors on /chat still return the graded JSON shape, never a bare 500."""
    return JSONResponse(status_code=200,
                        content={"reply": reply, "tool_calls": [], "tokens": {"in": 0, "out": 0}})


@app.exception_handler(RequestValidationError)
async def on_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path == "/chat":
        fields = ", ".join("JSON body" if e.get("type") == "json_invalid" else str(e["loc"][-1])
                           for e in exc.errors())
        return chat_error(f"Sorry, I couldn't read that request (check: {fields}). "
                          "Send JSON with user_id, thread_id and message.")
    # Model-level validators may include a ValueError in ctx, which is not JSON serializable.
    errors = [{k: v for k, v in error.items() if k != "ctx"} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(LoginRedirect)
async def on_login_redirect(request: Request, exc: LoginRedirect):
    return RedirectResponse(f"/login?next={request.url.path}", status_code=303)


@app.exception_handler(Exception)
async def on_unhandled(request: Request, exc: Exception):
    log.exception("unhandled error")
    if request.url.path == "/chat":
        return chat_error("Sorry, something went wrong on my side. Please try again.")
    return JSONResponse(status_code=500, content={"detail": "internal error"})


# ---------- Endpoints ----------

class ChatRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    thread_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    # Optional UI-button actions: publish/revise a chosen draft, or optimise a post.
    action: Literal["publish", "optimize", "revise"] | None = None
    target_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def revision_needs_draft(self):
        if self.action == "revise" and self.target_id is None:
            raise ValueError("Choose a draft to revise")
        return self

    @field_validator("user_id", "thread_id", "message", mode="before")
    @classmethod
    def strip(cls, v):
        if isinstance(v, (int, float)):
            v = str(v)
        if isinstance(v, str):
            # drop control chars except newline/tab
            v = "".join(ch for ch in v if ch in "\n\t" or ord(ch) >= 32).strip()
        return v


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[dict]
    tokens: dict[str, int]


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


def _action(req: ChatRequest) -> dict | None:
    if req.action == "revise":
        return {"type": "revise", "draft_id": req.target_id}
    if req.action == "publish":
        return {"type": "publish", "draft_id": req.target_id or 0}
    if req.action == "optimize":
        return {"type": "optimize", "post_id": req.target_id or 0}
    return None


def _gate(request: Request, req: ChatRequest) -> tuple[str | None, str]:
    """Auth + rate limit for a chat turn: (user_id, "") or (None, reply explaining why not)."""
    user_id, error = auth.resolve_api_user(request, req.user_id)
    if not user_id:
        return None, error
    if not rate_limit.allow_chat(request, user_id):
        return None, rate_limit.RATE_LIMITED
    return user_id, ""


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    # Sync endpoint: FastAPI runs it in a threadpool; the graph uses a sync Postgres pool.
    user_id, error = _gate(request, req)
    if not user_id:
        return chat_error(error)
    return graph.run_turn(user_id, req.thread_id, req.message, _action(req))


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, request: Request) -> StreamingResponse:
    """Same as /chat, but streams step-by-step progress so a ~45s article does not look hung."""
    user_id, error = _gate(request, req)

    def sse():
        if not user_id:
            done = {"reply": error, "tool_calls": [], "tokens": {"in": 0, "out": 0}, "reply_html": render_html(error)}
            yield f"event: done\ndata: {json.dumps(done)}\n\n"
            return
        for event, data in graph.stream_turn(user_id, req.thread_id, req.message, _action(req)):
            if event == "keepalive":
                yield ": keepalive\n\n"
                continue
            if event == "done":
                data = {**data, "reply_html": render_html(data["reply"])}   # escaped Markdown, for the web UI
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class ResumeRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    resume_token: str = Field(min_length=8, max_length=128)
    decision: Literal["approve", "reject", "amend"]
    # only used with decision="amend": change what gets published
    title: str | None = Field(default=None, max_length=120)
    slug: str | None = Field(default=None, max_length=80)
    meta_description: str | None = Field(default=None, max_length=155)


@app.post("/resume", response_model=ChatResponse)
def resume(req: ResumeRequest, request: Request):
    """Resolve a pending approval. Works exactly once, and only for the user it belongs to."""
    user_id, error = auth.resolve_api_user(request, req.user_id)
    if not user_id:
        return JSONResponse(status_code=401, content={"detail": error})
    amend = {k: v for k, v in {"title": req.title, "slug": req.slug,
                               "meta_description": req.meta_description}.items() if v} if req.decision == "amend" else {}
    out = graph.resume_turn(user_id, req.resume_token, req.decision, amend)
    if request.headers.get("x-rankforge-ui"):
        out = {**out, "reply_html": render_html(out["reply"])}
    return JSONResponse(out)


@app.get("/approvals")
def pending_approvals(request: Request, user_id: str = "") -> dict:
    uid, error = auth.resolve_api_user(request, user_id[:64])
    if not uid:
        raise HTTPException(status_code=401, detail=error)
    rows = approvals.list_pending(uid)
    return {"approvals": [{"id": r["id"], "resume_token": r["resume_token"], "action": r["action"],
                           "thread_key": r["thread_key"], "created_at": str(r["created_at"])} for r in rows]}


app.include_router(public_router)
app.include_router(auth_router)
app.include_router(app_router)
app.include_router(admin_router)
app.include_router(cron_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


@app.get("/")
def landing(request: Request):
    return render(request, "landing.html")


@app.get("/api")
def api_index() -> dict:
    return {"name": "RankForge Agent API",
            "endpoints": ["POST /chat", "POST /chat/stream", "POST /resume", "GET /approvals", "GET /health",
                          "GET /posts/{slug}", "GET /r/{code}", "GET /sitemap.xml", "GET /robots.txt",
                          "POST /cron/run"]}
