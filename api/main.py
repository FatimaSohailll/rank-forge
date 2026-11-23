"""RankForge agent API.

POST /chat        {"user_id", "thread_id", "message"} -> {"reply", "tool_calls", "tokens": {"in", "out"}}
POST /chat/stream same request, Server-Sent Events: `progress` per finished step, then `done` (same body as /chat)
POST /resume      approve / reject / amend a pending publish request by resume_token
GET  /approvals   pending approvals for a user
GET  /posts/{slug}, /r/{code}, /sitemap.xml, /robots.txt   public pages and tracked affiliate redirects
GET  /health -> ok
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from config import settings
import graph
from stores import approvals
from web.public import router as public_router

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("rankforge")


@asynccontextmanager
async def lifespan(app: FastAPI):
    graph.init()
    yield
    graph.shutdown()


app = FastAPI(title="RankForge Agent API", version="0.1.0", lifespan=lifespan)

# ---------- HTTP middleware ----------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
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
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


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


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> dict:
    # Sync endpoint: FastAPI runs it in a threadpool; the graph uses a sync Postgres pool.
    return graph.run_turn(req.user_id, req.thread_id, req.message)


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """Same as /chat, but streams step-by-step progress so a ~45s article does not look hung."""
    def sse():
        for event, data in graph.stream_turn(req.user_id, req.thread_id, req.message):
            if event == "keepalive":
                yield ": keepalive\n\n"
            else:
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
def resume(req: ResumeRequest) -> dict:
    """Resolve a pending approval. Works exactly once, and only for the user it belongs to."""
    amend = {k: v for k, v in {"title": req.title, "slug": req.slug,
                               "meta_description": req.meta_description}.items() if v} if req.decision == "amend" else {}
    return graph.resume_turn(req.user_id, req.resume_token, req.decision, amend)


@app.get("/approvals")
def pending_approvals(user_id: str) -> dict:
    rows = approvals.list_pending(user_id[:64])
    return {"approvals": [{"id": r["id"], "resume_token": r["resume_token"], "action": r["action"],
                           "created_at": str(r["created_at"])} for r in rows]}


app.include_router(public_router)


@app.get("/")
def root() -> dict:
    return {"name": "RankForge Agent API",
            "endpoints": ["POST /chat", "POST /chat/stream", "POST /resume", "GET /approvals", "GET /health",
                          "GET /posts/{slug}", "GET /r/{code}", "GET /sitemap.xml", "GET /robots.txt"]}
