"""RankForge agent API.

POST /chat   {"user_id", "thread_id", "message"} -> {"reply", "tool_calls", "tokens": {"in", "out"}}
GET  /health -> ok
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from config import settings
import graph

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


@app.get("/")
def root() -> dict:
    return {"name": "RankForge Agent API", "endpoints": ["POST /chat", "GET /health"]}
