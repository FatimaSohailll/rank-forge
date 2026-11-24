"""Per-run context: who is asking, and what this turn has spent.

Set once per /chat call. Tools and LLM wrappers read user_id from here — never
from model-generated arguments — which is what keeps users isolated.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RunContext:
    run_id: str
    user_id: str
    thread_id: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    searches: int = 0           # Tavily credits spent this run
    tool_calls: list[dict] = field(default_factory=list)

    def add_usage(self, tokens_in: int, tokens_out: int, cost: float) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost

    def add_tool_call(self, name: str, args: dict) -> None:
        self.tool_calls.append({"name": name, "args": args})


_current: ContextVar[RunContext | None] = ContextVar("rankforge_run", default=None)


def set_run(ctx: RunContext):
    return _current.set(ctx)


def reset_run(token) -> None:
    _current.reset(token)


def current() -> RunContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("no active RunContext")
    return ctx
