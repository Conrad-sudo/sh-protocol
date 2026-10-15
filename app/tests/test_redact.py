"""
Checks that an RPC provider's API key never leaves the process: not in an HTTP error, not in the
text the model reads, not in a log line.

The key rides in the RPC URL, and web3 and requests put the URL in their exception text, so every
path that turns an exception into text is tried here with one that carries a key. Offline.

Run: make redact-test   (or: python app/tests/test_redact.py)
"""
import io
import logging
import os
import tempfile

from checks import check, finish, set_everywhere   # first: it puts app/ on sys.path for the imports below

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production-0123456789abcdef")

import db                                   # noqa: E402
db.DB_PATH = _tmp_db.name
db.init_db()

import requests                             # noqa: E402
from fastapi import HTTPException           # noqa: E402
from langchain_core.tools import ToolException  # noqa: E402

import api                                  # noqa: E402
import routes.deps  # noqa: E402
import explorers                            # noqa: E402
import redact                               # noqa: E402
import smart_wallet_agent                   # noqa: E402
import tools                                # noqa: E402

KEY = "AbCdEf0123456789GhIjKl"
URL = f"https://eth-sepolia.g.alchemy.com/v2/{KEY}"
# What requests actually says when the node can't be reached: the host and the path, apart.
CONNECTION_ERROR = (
    "HTTPSConnectionPool(host='eth-sepolia.g.alchemy.com', port=443): Max retries exceeded with "
    f"url: /v2/{KEY} (Caused by NewConnectionError('Failed to establish a new connection'))"
)
TX_HASH = "0x" + "ab" * 32
ADDRESS = "0x" + "cd" * 20


def test_redact():
    print("\n[1] redact() masks keys, and only keys")
    check("a key in an Alchemy-style URL", KEY not in redact.redact(URL) and "eth-sepolia.g.alchemy.com" in redact.redact(URL),
          redact.redact(URL))
    check("a key in requests' connection error", KEY not in redact.redact(CONNECTION_ERROR), redact.redact(CONNECTION_ERROR))
    masked = redact.redact(f"GET https://api.etherscan.io/v2/api?module=account&apikey={KEY}&page=1")
    check("an apikey= query value", KEY not in masked and "module=account" in masked and "page=1" in masked, masked)
    masked = redact.redact("https://alice:hunter2-but-longer@node.example.com/rpc")
    check("a user:password@ prefix", "hunter2" not in masked and "node.example.com" in masked, masked)

    os.environ["TEST_PROVIDER_API_KEY"] = "zz-provider-secret-value"
    os.environ["TEST_RPC_URL"] = "https://quick.example.pro/0123456789abcdef0123456789abcdef/"
    try:
        masked = redact.redact("refused: zz-provider-secret-value")
        check("the value of a *_KEY variable", "zz-provider-secret-value" not in masked, masked)
        masked = redact.redact("Max retries exceeded with url: /0123456789abcdef0123456789abcdef/")
        check("the key segment of an *_RPC_URL variable, even with the host gone", "0123456789abcdef" not in masked, masked)
    finally:
        del os.environ["TEST_PROVIDER_API_KEY"], os.environ["TEST_RPC_URL"]

    plain = f"tx {TX_HASH} from {ADDRESS} reverted: SessionHandler_NotEnoughBalance()"
    check("transaction hashes, addresses and error names are left alone", redact.redact(plain) == plain, redact.redact(plain))
    check("a public RPC URL is left alone",
          redact.redact("https://ethereum-sepolia-rpc.publicnode.com") == "https://ethereum-sepolia-rpc.publicnode.com")


def test_log_filter():
    print("\n[2] logs: the message, its arguments and the traceback are masked")
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.addFilter(redact.RedactingFilter())
    logger = logging.getLogger("test_redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("Cannot reach %s", URL)
        logger.warning("Failed: %s", requests.ConnectionError(CONNECTION_ERROR))
        try:
            raise requests.ConnectionError(CONNECTION_ERROR)
        except requests.ConnectionError:
            logger.exception("Agent turn failed")
    finally:
        logger.removeHandler(handler)
    text = out.getvalue()
    check("nothing logged carries the key", KEY not in text, text[:300])
    check("...while the lines and the traceback are still there",
          "Cannot reach" in text and "Agent turn failed" in text and "Traceback" in text, text[:300])
    check("the API installs the filter where unconfigured loggers end up",
          any(isinstance(f, redact.RedactingFilter) for f in logging.lastResort.filters))


def test_error_paths():
    print("\n[3] every path that turns an exception into text masks it")
    error = requests.ConnectionError(CONNECTION_ERROR)

    class Unreachable:
        @property
        def chain_id(self):
            raise error

    original = routes.deps.load_network_config_by_name
    set_everywhere(api, "load_network_config_by_name", lambda _name: (type("W3", (), {"eth": Unreachable()})(), None))
    try:
        try:
            routes.deps._resolve_chain(11155111)
            detail = None
        except HTTPException as e:
            detail = e
    finally:
        set_everywhere(api, "load_network_config_by_name", original)
    check("an unreachable RPC -> 503 that names the network, not the URL",
          detail is not None and detail.status_code == 503 and KEY not in detail.detail and "alchemy" not in detail.detail,
          str(detail and detail.detail))

    check("the owner simulations' revert reason", KEY not in routes.deps._revert_reason(error), routes.deps._revert_reason(error))
    check("what the model reads when a tool raises", KEY not in smart_wallet_agent._tool_failure_message(error),
          smart_wallet_agent._tool_failure_message(error))
    check("what the model reads when a tool refuses",
          KEY not in tools._tool_error_text(ToolException(f"Could not send: {CONNECTION_ERROR}")))
    check("every tool the agent gets refuses through that",
          all(t.handle_tool_error is tools._tool_error_text for t in tools.get_tools()))
    check("the block explorers' errors", KEY not in str(explorers.ExplorerUnavailable(f"Alchemy: {CONNECTION_ERROR}")))


if __name__ == "__main__":
    try:
        test_redact()
        test_log_filter()
        test_error_paths()
    finally:
        os.unlink(_tmp_db.name)

    finish("All redaction checks passed.")
