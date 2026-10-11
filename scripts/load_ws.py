"""Load test for the chat websocket (VOTEBOT-34): is one uvicorn worker enough, and what does a second cost?

Starts `scripts/load_app.py` (the real app, LLM stubbed) with --workers N on a free local port, opens C websockets at
once, each asking the same three questions in turn (a quick-action button, a typed question, a "what changed" one), and
reports per-question latency, errors, how long a trivial request (/health/live) took while the load ran (a blocked event
loop shows up there), and peak memory of the server's processes.

    python scripts/load_ws.py --workers 1 --clients 25
    python scripts/load_ws.py --workers 1 2 3 --clients 10 25 50 100     # the whole matrix, one table row per run

Needs the .env keys (Pinecone, OpenAI embeddings, api-v3), a Redis on localhost:6380 (the dev one) and a bill in the
index, like smoke_ws.py, whose `discover_bill` it uses. Do NOT point it at production: use `--url` only for a dev copy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_ws import discover_bill  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
QUESTIONS = [
    ("button", "Summarize this bill", "summary"),
    ("typed", "What does this bill do?", None),
    ("changed", "What changed in this bill since the previous version?", None),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def tree_stats(root_pid: int) -> tuple[float, float]:
    """(resident MiB, %cpu) summed over the server's process tree (uvicorn's workers are its children)."""
    out = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,%cpu="], capture_output=True, text=True).stdout
    rows = [tuple(map(float, line.split())) for line in out.splitlines() if line.strip()]
    pids, grew = {root_pid}, True
    while grew:
        grew = False
        for pid, ppid, _, _ in rows:
            if ppid in pids and pid not in pids:
                pids.add(pid)
                grew = True
    mine = [r for r in rows if r[0] in pids]
    return sum(r[2] for r in mine) / 1024, sum(r[3] for r in mine)


async def one_client(url: str, page_context: dict, results: list, errors: list, workers_seen: list, delay: float) -> None:
    await asyncio.sleep(delay)  # visitors do not all arrive in the same instant (and then one worker would accept them all)
    try:
        async with websockets.connect(url, open_timeout=30, max_size=None) as conn:
            for kind, message, button in QUESTIONS:
                payload = {"message": message, "page_context": page_context}
                if button:
                    payload["button"] = button
                started = time.perf_counter()
                await conn.send(json.dumps({"type": "user_message", "payload": payload}))
                while True:
                    frame = json.loads(await asyncio.wait_for(conn.recv(), 120))
                    if frame.get("type") == "stream_chunk":
                        found = re.match(r"\[worker (\d+)\]", frame["payload"].get("text", ""))
                        if found:
                            workers_seen.append(found.group(1))
                    if frame.get("type") == "stream_end":
                        results.append((kind, time.perf_counter() - started))
                        break
                    if frame.get("type") == "error":
                        errors.append(f"{kind}: {frame.get('payload')}")
                        break
    except Exception as e:  # noqa: BLE001 -- a failed connection is a result, not a crash
        errors.append(f"connection: {type(e).__name__}: {e}")


async def probe(http_base: str, stop: asyncio.Event, lags: list, errors: list) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        while not stop.is_set():
            started = time.perf_counter()
            try:
                (await client.get(f"{http_base}/votebot/v1/health/live")).raise_for_status()
                lags.append(time.perf_counter() - started)
            except Exception as e:  # noqa: BLE001
                errors.append(f"probe: {type(e).__name__}")
            await asyncio.sleep(0.1)


async def sample(pid: int, stop: asyncio.Event, samples: list) -> None:
    while not stop.is_set():
        samples.append(tree_stats(pid))
        await asyncio.sleep(0.5)


def start_server(workers: int, port: int) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "load_app:app", "--app-dir", str(REPO / "scripts"), "--port", str(port),
         "--workers", str(workers), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )


async def wait_ready(http_base: str, server: subprocess.Popen, seconds: int = 90) -> None:
    async with httpx.AsyncClient(timeout=3) as client:
        for _ in range(seconds):
            if server.poll() is not None:
                raise RuntimeError("the load-test server exited at startup (run it by hand to see why)")
            try:
                if (await client.get(f"{http_base}/votebot/v1/health/live")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError("the load-test server did not come up")


async def run(workers: int, clients: int, page_context: dict, ramp: float):
    port = free_port()
    http_base, ws_url = f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}/ws/chat"
    server = start_server(workers, port)
    try:
        await wait_ready(http_base, server)
        results: list = []
        errors: list = []
        lags: list = []
        samples: list = []
        workers_seen: list = []
        stop = asyncio.Event()
        watchers = [asyncio.create_task(probe(http_base, stop, lags, errors)), asyncio.create_task(sample(server.pid, stop, samples))]
        started = time.perf_counter()
        await asyncio.gather(
            *(one_client(ws_url, page_context, results, errors, workers_seen, ramp * i / clients) for i in range(clients))
        )
        elapsed = time.perf_counter() - started
        stop.set()
        await asyncio.gather(*watchers)
    finally:
        os.killpg(server.pid, signal.SIGTERM)
        server.wait(timeout=30)

    def col(kind: str) -> str:
        values = [seconds for k, seconds in results if k == kind]
        return f"{statistics.median(values):.2f} / {pct(values, 0.95):.2f}" if values else "-"

    served = sorted((workers_seen.count(pid) for pid in set(workers_seen)), reverse=True)  # answers per worker
    peak_mib = max((s[0] for s in samples), default=float("nan"))
    mean_cpu = statistics.mean(s[1] for s in samples) if samples else float("nan")
    return (
        f"| {workers} | {clients} | {col('button')} | {col('typed')} | {col('changed')} | "
        f"{statistics.median(lags) * 1000:.0f} / {pct(lags, 0.95) * 1000:.0f} / {max(lags, default=0) * 1000:.0f} | "
        f"{len(errors)} of {clients * len(QUESTIONS)} | {'/'.join(map(str, served)) or '-'} | {peak_mib:.0f} | {mean_cpu:.0f} | {elapsed:.0f} |"
    ), errors


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, nargs="+", default=[1])
    parser.add_argument("--clients", type=int, nargs="+", default=[10])
    parser.add_argument("--ramp", type=float, default=3.0, help="seconds over which the clients connect (default 3)")
    parser.add_argument("--jurisdiction", default="FL", help="the bill's jurisdiction (an embedded bill is discovered)")
    args = parser.parse_args(argv)

    page_context = await discover_bill(args.jurisdiction)
    print(f"bill: {page_context['id']} {page_context['jurisdiction']} {page_context['session']}\n")
    print("p50 / p95 seconds per question; health/live is p50 / p95 / max milliseconds while loaded\n")
    print("| workers | clients | button | typed | what changed | health/live ms | errors | answers per worker | peak MiB | cpu % | s |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    all_errors: list = []
    for workers in args.workers:
        for clients in args.clients:
            row, errors = await run(workers, clients, page_context, args.ramp)
            print(row, flush=True)
            all_errors += errors[:3]
    for e in all_errors:
        print("error:", e)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
