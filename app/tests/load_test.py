"""
How many chat turns one API process carries at once, and how much of its CPU each one takes.

The API runs as one process (`make api`: --workers 1), so every turn shares one Python interpreter
-- and, for Python code, one core at a time. This measures how close that comes to being the limit.
Three processes, so only the API's own work is counted:

  - a stand-in for DeepSeek that speaks its API. It answers each call after MODEL_SECS: first with a
    call to the get_all_contacts tool, then with a reply -- the two model calls a turn makes;
  - the real API, as `make api-prod` runs it, on a scratch database, with DEEPSEEK_API_BASE pointed
    at the stand-in and CHAT_MAX_CONCURRENT raised to the highest level, so the cap refuses none.
    The routes, tokens, rate limits, agent, checkpointer and DeepSeek client are all the real ones;
  - this one. It sends chats from many accounts at once, at each level in turn, and reads the API's
    CPU time from `ps` before and after each level.

Left out: chain reads (the tool reads the database only) and Vault. A real turn makes a dozen or
more RPC reads, so it costs the API more CPU than a turn here: measured on a mainnet fork
(2026-10-19), 50-200 ms a turn against about 28 ms here.

Run: make load-test   (or: python app/tests/load_test.py [turns-at-once ...], default 8 32 64 128)
"""
import asyncio
import collections
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# How long the stand-in takes to answer each model call. A live deepseek-flash call took 1-2 s.
MODEL_SECS = 1.5

# How long each level sends new chats for. Turns still running then are waited for, and counted.
STAGE_SECS = 40

# Accounts per simulated user, which it takes turns with. A turn here lasts about 2 x MODEL_SECS, so
# one account alone would pass the 20-a-minute limit (chat_limits.CHAT_RATE_LIMIT) and be refused.
ACCOUNTS_PER_USER = 2

LEVELS = (8, 32, 64, 128)
CHAIN_ID = 11155111
MESSAGE = "Who are my contacts?"
REPLY = "You have no saved contacts yet. Add one on the Contacts page, then ask me to send to it."
JWT_SECRET = "load-test-secret-not-for-production-0123456789abcdef"


def _model_stand_in(port: int) -> None:
    """Serves DeepSeek's chat completions API on `port`: a tool call first, then the reply."""
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    calls = 0

    async def completions(request: Request) -> JSONResponse:
        nonlocal calls
        calls += 1
        body = await request.json()
        await asyncio.sleep(MODEL_SECS)
        if body["messages"][-1]["role"] == "tool":
            message, finish = {"role": "assistant", "content": REPLY}, "stop"
        else:
            call = {"id": f"call_{calls}", "type": "function",
                    "function": {"name": "get_all_contacts", "arguments": "{}"}}
            message, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        return JSONResponse({
            "id": f"load-{calls}", "object": "chat.completion", "created": int(time.time()),
            "model": body["model"],
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 18_000, "completion_tokens": 40, "total_tokens": 18_040},
        })

    app = Starlette(routes=[Route("/v1/chat/completions", completions, methods=["POST"])])
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(port: int, process: subprocess.Popen, name: str, log: Path) -> None:
    """Returns once `port` takes connections: uvicorn opens it after the app's startup has run."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit(f"The {name} exited while starting. Its log: {log}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"The {name} did not start within 90 s. Its log: {log}")


def _cpu_secs(pid: int) -> float:
    """The CPU time, user and system, that `pid` has used so far, from ps ("1:02.34" or "1:02:03")."""
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, check=True)
    *larger, seconds = out.stdout.strip().split(":")
    total = float(seconds)
    for scale, part in zip((60, 3600), reversed(larger)):
        total += scale * int(part)
    return total


async def _level(client, url: str, users: list[list[str]]) -> tuple[list[float], collections.Counter]:
    """
    One level: every simulated user sends a chat, waits for the reply and sends the next, from its
    next account, until STAGE_SECS have passed. Returns each answered turn's time and the failures.
    """
    import httpx

    times: list[float] = []
    failures: collections.Counter = collections.Counter()
    stop_at = time.monotonic() + STAGE_SECS

    async def user(tokens: list[str]) -> None:
        sent = 0
        while time.monotonic() < stop_at:
            headers = {"Authorization": f"Bearer {tokens[sent % len(tokens)]}"}
            sent += 1
            started = time.monotonic()
            try:
                r = await client.post(url, json={"chain_id": CHAIN_ID, "message": MESSAGE}, headers=headers)
            except httpx.HTTPError as err:
                failures[type(err).__name__] += 1
            else:
                if r.status_code != 200:
                    failures[f"HTTP {r.status_code}"] += 1
                elif r.json()["reply"] != REPLY:
                    failures["turn failed"] += 1
                else:
                    times.append(time.monotonic() - started)
                    continue
            await asyncio.sleep(1)   # a refusal comes back at once; don't spin on it

    await asyncio.gather(*(user(tokens) for tokens in users))
    return times, failures


async def _run(api_port: int, api_pid: int, model_pid: int, warm_up: list[int],
               accounts: dict[int, list[list[int]]]) -> bool:
    """Runs every level and prints a row for each. Returns whether every turn was answered."""
    import httpx

    import auth

    url = f"http://127.0.0.1:{api_port}/api/chat"
    floor = 2 * MODEL_SECS
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    async with httpx.AsyncClient(timeout=180, limits=limits) as client:
        # Warm up: the first turns pay for what is built once (compiled schemas, caches, connections).
        await asyncio.gather(*(
            client.post(url, json={"chain_id": CHAIN_ID, "message": MESSAGE},
                        headers={"Authorization": f"Bearer {auth.create_access_token(user_id)}"})
            for user_id in warm_up
        ))

        print(f"\nEach model call takes {MODEL_SECS} s, so a turn takes at least {floor} s.\n")
        print(f"{'at once':>8} {'turns':>6} {'turns/s':>8} {'median':>7} {'p95':>7} {'added':>7} "
              f"{'API CPU':>8} {'CPU/turn':>9} {'stand-in':>9}  failures")
        all_answered = True
        for level, users in accounts.items():
            # Minted per level: an access token lasts 15 minutes.
            tokens = [[auth.create_access_token(a) for a in user] for user in users]
            api_before, model_before, started = _cpu_secs(api_pid), _cpu_secs(model_pid), time.monotonic()
            times, failures = await _level(client, url, tokens)
            wall = time.monotonic() - started
            api_cpu, model_cpu = _cpu_secs(api_pid) - api_before, _cpu_secs(model_pid) - model_before
            if not times:
                print(f"{level:>8} {0:>6}  no turn answered  {dict(failures)}")
                all_answered = False
                continue
            median = statistics.median(times)
            p95 = statistics.quantiles(times, n=20)[18] if len(times) > 1 else times[0]
            print(f"{level:>8} {len(times):>6} {len(times) / wall:>8.1f} {median:>6.2f}s {p95:>6.2f}s "
                  f"{median - floor:>6.2f}s {100 * api_cpu / wall:>7.0f}% {1000 * api_cpu / len(times):>7.0f}ms "
                  f"{100 * model_cpu / wall:>8.0f}%  {dict(failures) or '-'}")
            all_answered = all_answered and not failures
    print("\nat once:  turns running together.  added:  the median turn's time over the floor.")
    print("API CPU:  of one core.  CPU/turn:  the API's CPU time per answered turn.")
    print("stand-in: the model stand-in's CPU, of one core (it must stay well short of 100%).")
    return all_answered


def main(levels: list[int]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="mitfah-load-"))
    # Before the app modules import: they read both at import.
    os.environ["APP_DB_PATH"] = str(workdir / "load.db")
    os.environ["JWT_SECRET"] = JWT_SECRET
    sys.path.insert(0, str(ROOT / "app"))
    import db

    db.init_db()
    def account() -> int:
        return db.create_user("0x" + os.urandom(20).hex())

    warm_up = [account() for _ in range(4)]
    # Fresh accounts for every level, so one level's history and limits don't carry into the next.
    accounts = {level: [[account() for _ in range(ACCOUNTS_PER_USER)] for _ in range(level)] for level in levels}

    model_port, api_port = _free_port(), _free_port()
    model_log, api_log = workdir / "stand-in.log", workdir / "api.log"
    env = {
        **os.environ,
        "DEEPSEEK_API_BASE": f"http://127.0.0.1:{model_port}/v1",
        "DEEPSEEK_API_KEY": "load-test",
        # This measures the process, not the cap: room for the highest level, so none is refused.
        "CHAT_MAX_CONCURRENT": str(max(levels)),
    }
    with open(model_log, "w") as model_out, open(api_log, "w") as api_out:
        model = subprocess.Popen([sys.executable, __file__, "--model-stand-in", str(model_port)],
                                 stdout=model_out, stderr=subprocess.STDOUT)
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--app-dir", "app", "api:app", "--workers", "1",
             "--host", "127.0.0.1", "--port", str(api_port)],
            cwd=ROOT, env=env, stdout=api_out, stderr=subprocess.STDOUT,
        )
        try:
            _wait_for(model_port, model, "model stand-in", model_log)
            _wait_for(api_port, api, "API", api_log)
            answered = asyncio.run(_run(api_port, api.pid, model.pid, warm_up, accounts))
        finally:
            for process in (api, model):
                process.terminate()
            for process in (api, model):
                process.wait(timeout=30)
    if not answered:
        raise SystemExit(f"\nSome turns failed. The API's log: {api_log}")
    shutil.rmtree(workdir)
    print("\nEvery turn was answered.")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--model-stand-in"]:
        _model_stand_in(int(sys.argv[2]))
    else:
        main([int(arg) for arg in sys.argv[1:]] or list(LEVELS))
