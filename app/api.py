"""
The HTTP API for the web app: builds the FastAPI app and includes every router in routes/.

Run with `make api` (uvicorn --app-dir app api:app --workers 1). One worker on purpose: the bundler's
nonces and the chat's rationing live in this process.
"""
import os
from contextlib import asynccontextmanager

import anyio.to_thread
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import routes.added_tokens
import routes.allowlist
import routes.catalog
import routes.chat
import routes.contacts
import routes.deploy
import routes.history
import routes.owner
import routes.passkeys
import routes.sign
import routes.signin
import routes.telegram_link
import routes.wallet
from chat_limits import CONFIRM_MAX_CONCURRENT
from redact import install_log_redaction
from routes.chat import CHAT_MAX_CONCURRENT
from routes.deps import limiter
from smart_wallet_agent import (
    close_checkpointer,
    init_agent,
    open_checkpointer,
)

load_dotenv()


# uvicorn configures its log handlers before it imports this module, so they exist by now.
install_log_redaction()

# The threads every plain-`def` route runs on. anyio's default is 40; chat turns and passkey
# approvals hold up to CHAT_MAX_CONCURRENT + CONFIRM_MAX_CONCURRENT of them, for seconds at a time,
# so the pool is that plus the default's 40 for everything else -- the dashboard, sign-in, History.
API_THREADS = CHAT_MAX_CONCURRENT + CONFIRM_MAX_CONCURRENT + 40


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    Sizes the route threads (API_THREADS), opens the agent's checkpointer and builds the agent on
    startup, closing it on shutdown.

    Exactly what telebot.py does around its bot. Without it `agent` stays None and every
    /api/chat request fails on the first attribute access.
    """
    anyio.to_thread.current_default_thread_limiter().total_tokens = API_THREADS
    await open_checkpointer()
    init_agent()
    try:
        yield
    finally:
        await close_checkpointer()


app = FastAPI(lifespan=lifespan)


app.state.limiter = limiter


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# The refresh token travels as a cookie, so the browser must be allowed to send credentials --
# which means the allowed origins have to be listed explicitly (a wildcard is rejected by the
# browser alongside credentials, and would be wrong here anyway).
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(routes.signin.router)
app.include_router(routes.telegram_link.router)
app.include_router(routes.contacts.router)
app.include_router(routes.chat.router)
app.include_router(routes.history.router)
app.include_router(routes.catalog.router)
app.include_router(routes.added_tokens.router)
app.include_router(routes.deploy.router)
app.include_router(routes.wallet.router)
app.include_router(routes.owner.router)
app.include_router(routes.allowlist.router)
app.include_router(routes.passkeys.router)
app.include_router(routes.sign.router)
