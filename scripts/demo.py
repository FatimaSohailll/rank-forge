"""Run the RankForge agent graph from the terminal, with live progress.

One-shot:     python scripts/demo.py "write an article on best budget camping tents for families"
Interactive:  python scripts/demo.py            (type messages; /quit to exit; the thread keeps its memory)
Options:      --user demo_alice  --thread my-thread  --db  (use DATABASE_URL instead of in-memory state)

By default it runs without the database (in-memory checkpoints and stores), so it only needs
ANTHROPIC_API_KEY and TAVILY_API_KEY in .env.
"""
import argparse
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))


def run(graph, user: str, thread: str, message: str) -> None:
    print(f"\n> {message}\n")
    for event, data in graph.stream_turn(user, thread, message):
        if event == "progress":
            print(f"  [{data['seconds']:5.1f}s] {data['label']}", flush=True)
        elif event == "done":
            tools = {}
            for call in data["tool_calls"]:
                tools[call["name"]] = tools.get(call["name"], 0) + 1
            print("\n" + "=" * 70 + "\n" + data["reply"] + "\n" + "=" * 70)
            cost = (data["tokens"]["in"] * 0.10 + data["tokens"]["out"] * 0.50) / 1_000_000
            print(f"tools: {tools or 'none'} | tokens in/out: {data['tokens']['in']}/{data['tokens']['out']} "
                  f"| ~${cost:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("message", nargs="*", help="message to send (omit for interactive mode)")
    parser.add_argument("--user", default="demo_alice")
    parser.add_argument("--thread", default=f"demo-{uuid.uuid4().hex[:6]}")
    parser.add_argument("--db", action="store_true", help="use DATABASE_URL instead of in-memory state")
    args = parser.parse_args()

    if not args.db:
        os.environ["DATABASE_URL"] = ""          # env vars override .env

    import logging
    import graph
    graph.init()
    logging.getLogger().setLevel(logging.WARNING)
    try:
        if args.message:
            run(graph, args.user, args.thread, " ".join(args.message))
            return
        print(f"RankForge demo | user={args.user} thread={args.thread} | /quit to exit")
        while True:
            try:
                message = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if message in ("/quit", "/exit"):
                break
            if message:
                run(graph, args.user, args.thread, message)
    finally:
        graph.shutdown()


if __name__ == "__main__":
    main()
