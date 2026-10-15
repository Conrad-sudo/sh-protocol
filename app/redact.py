"""
Keeps API keys out of everything that leaves the process: HTTP error details, the text the model
reads, and the logs.

RPC providers put the key in the URL (Alchemy's /v2/<key>, NodeReal's /v1/<key>, Etherscan's
?apikey=), and web3 and requests put the URL in their exception text -- "Max retries exceeded with
url: /v2/<key>". So any `{e}` that reaches a client, the model or a log line can carry a key.
redact() masks two kinds of thing:

  - the values of the secrets this process was started with: every environment variable whose
    name marks it as one (a key, a token, a private key, an RPC URL). For a URL, only the parts
    that look like a credential are masked, so the host stays readable.
  - credential-shaped parts of any URL, configured or not -- the /vN/<key> path segment, an
    apikey= style query value, a user:password@ prefix. A key that is no longer in .env but is
    still in an old rpcs row is caught by these.

Transaction hashes and addresses are left alone: they are what makes an error worth reading.
"""
import logging
import os
import re
from urllib.parse import parse_qsl, urlsplit

# The mask, the same one explorers.py has always used for its keys.
MASK = "***"

# Names of environment variables that hold a secret, or a URL that may carry one.
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PRIVATE|BUNDLER|ROLE_ID|RPC_URL|DSN|_API$", re.I)
# Shorter values are switches and ports, not secrets, and masking them would garble ordinary text.
_MIN_SECRET_LEN = 8
# A path segment or query value at least this long, inside a configured URL, is treated as its key.
_MIN_URL_SECRET_LEN = 16

_URL_PATTERNS = (
    # /v2/<key> (Alchemy), /v3/<key> (Infura), /v1/<key> (NodeReal).
    (re.compile(r"(/v\d+/)[A-Za-z0-9_-]{16,}"), r"\1" + MASK),
    # ?apikey=<key>, &api_key=<key>, ?token=<key> ...
    (re.compile(r"([?&](?:api[_-]?key|key|token|access_token|auth)=)[^&\s'\"]+", re.I), r"\1" + MASK),
    # https://user:password@host
    (re.compile(r"(\w+://)[^/\s:@]+:[^/\s@]+@"), r"\1" + MASK + "@"),
)


def _url_secrets(url: str) -> list[str]:
    """The parts of a configured URL that look like a credential."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    found = [parts.password] if parts.password else []
    found += [seg for seg in parts.path.split("/") if len(seg) >= _MIN_URL_SECRET_LEN]
    found += [value for _, value in parse_qsl(parts.query) if len(value) >= _MIN_SECRET_LEN]
    return found


def _secrets() -> list[str]:
    """
    Every secret value in the environment, longest first, so a key that contains another is
    masked whole. Read on every call rather than cached: it costs microseconds, and a value set
    after startup (load_dotenv, a test) is then never missed.
    """
    found: set[str] = set()
    for name, value in os.environ.items():
        value = value.strip()
        if len(value) < _MIN_SECRET_LEN or not _SECRET_NAME.search(name):
            continue
        if "://" in value:
            found.update(_url_secrets(value))
        else:
            found.add(value)
    return sorted(found, key=len, reverse=True)


def redact(value) -> str:
    """
    `value` as text, with every secret and credential-shaped URL part replaced by MASK.

    @param value  Anything -- an exception is the usual case; it is passed through str().
    """
    text = str(value)
    for secret in _secrets():
        text = text.replace(secret, MASK)
    for pattern, replacement in _URL_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """
    Masks secrets in a log record before any handler writes it: the message, its arguments, and the
    traceback, which is where an RPC URL usually hides.

    Arguments stay arguments rather than being merged into the message, because some formatters
    read them positionally (uvicorn's access log does).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if isinstance(record.args, dict):
            record.args = {k: redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
        elif record.args:
            record.args = tuple(redact(a) if isinstance(a, (str, BaseException)) else a for a in record.args)
        if record.exc_info and not record.exc_text:
            # Formatted here so the masked text is what the handler prints: Formatter.format uses
            # exc_text as it finds it, and only formats exc_info itself when exc_text is empty.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


_FILTER = RedactingFilter()
# Where log records end up in this app: the root logger's handlers (if anything configured them),
# uvicorn's own handlers, which print every unhandled exception in a request, and Python's
# last-resort handler, which prints a WARNING or worse when no handler is configured at all --
# the case for smart_wallet_agent's and tx_history's loggers.
_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access", "telegram", "httpx")


def install_log_redaction():
    """
    Adds the filter to every handler a record can reach. Idempotent. Call it once logging is
    configured -- for the API that is at import, since uvicorn configures logging before it imports
    the app.
    """
    handlers = [logging.lastResort] if logging.lastResort else []
    for name in _LOGGERS:
        handlers += logging.getLogger(name).handlers
    for handler in handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)
