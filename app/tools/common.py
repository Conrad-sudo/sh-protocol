"""
What every tool shares: amounts, token and contact resolution, slippage, and the legs a transaction
moves. Nothing here imports another tools module.

There is no session key here any more: the server holds none. A transaction is approved by the
user's passkey on their own device (signing.py), and which passkeys the wallet approves is read off
the wallet itself where a check needs it (tools.checks).
"""
import re
from typing import NamedTuple

from langchain_core.tools import ToolException
from langchain_erc20.amounts import to_base_units
from web3 import Web3
from web3.contract import Contract

from constants import (
    ETH_SENTINEL,
    SESSION_EXPIRY_MARGIN_SECS,  # noqa: F401 -- re-exported: tools.checks and the tests read it here
    WEI_PER_ETH,
    get_native_asset_ticker,
    get_native_wrapped_ticker,
)
from contracts import (
    load_ierc20,
    load_session_handler,
    read_spending_config,
)
from db import (
    get_contact as _get_contact,
    is_contact_signed as _is_contact_signed,
)
from db import (
    get_custom_token as _get_custom_token,
)
from db import (
    get_supported_tokens as _get_supported_tokens,
)
from db import (
    get_supported_tokens_by_chain_id as _get_listed_tokens,
)
from db import (
    resolve_token as _resolve_token,
)
from network_config import load_network_config
from parallel import read_all


def _to_base_units(amount: float | str, decimals: int) -> int:
    """Whole units -> base units, via langchain-erc20's converter.

    Thin wrapper rather than a direct call: the package returns (base_units, truncated) and
    this codebase only ever wants the integer. Worth delegating anyway — the package rejects
    negative, non-finite and >uint256 amounts as ToolException instead of letting them reach
    web3 as an unreadable encoding error, and truncates toward zero so an over-precise amount
    can never round UP past a balance or an allowance.
    """
    base_units, _truncated = to_base_units(amount, decimals)
    return base_units


# An amount the caller gave within 1/ALL_OF_IT of a balance means all of it. The agent reads a
# balance as a whole-unit float (get_erc20_balance), and a float keeps ~16 significant digits, so
# "send all my LINK" comes back up to ~2.2e-16 of an 18-decimal balance away from it: above it
# (refused) or below it (dust left behind). Nobody means "all but a trillionth". The exchange
# toolkits settle the amounts they're given the same way (langchain-uniswap-v2 0.5.2+ and
# langchain_uniswap_v3), and tools.checks settles a sent leg the same way, so they all agree.
ALL_OF_IT = 10**12


def _snap_to_balance(amount_base: int, balance: int) -> int:
    """balance when amount_base is within a trillionth of it, else amount_base. Only for an amount
    the caller gave, never one worked out from a quote."""
    if abs(amount_base - balance) * ALL_OF_IT <= balance:
        return balance
    return amount_base


def _settle_amount(erc20: Contract, holder: str, amount: float, *, spender: str | None = None) -> int:
    """
    The base units to move for `amount`, which the caller gave in whole units: all of holder's
    balance when amount is within a trillionth of it (_snap_to_balance), and -- when `spender`
    moves it on holder's approval -- all of the allowance when it is within a trillionth of that.
    Otherwise amount as given. The ERC-20 only; the native asset never settles, since sending all of
    it would leave nothing for the fees.
    """
    holder = Web3.to_checksum_address(holder)
    reads = {"decimals": erc20.functions.decimals().call, "balance": erc20.functions.balanceOf(holder).call}
    if spender is not None:
        reads["allowance"] = erc20.functions.allowance(holder, Web3.to_checksum_address(spender)).call
    got = read_all(reads)
    amount_base = _snap_to_balance(_to_base_units(amount, got["decimals"]), got["balance"])
    if spender is not None:
        amount_base = _snap_to_balance(amount_base, got["allowance"])
    return amount_base


# The plan roles of the approvals a package adds around the real step: granting the allowance, and
# clearing what is left of it (langchain-erc20 / -uniswap-v2 / -erc8004 all use these two names).
_APPROVAL_ROLES = frozenset({"approve", "approve_reset"})


def _native_units(wei: int) -> str:
    """Wei as whole units of the native asset, to 8 decimal places at most (e.g. "0.00021")."""
    return f"{wei / WEI_PER_ETH:.8f}".rstrip("0").rstrip(".")


# Kept only as the default for the agent-facing slippage_bps arguments. The bounds themselves
# are derived inside langchain-uniswap-v2, in exact integer arithmetic -- the old float
# `int(base * (BPS - bps) / BPS)` here silently drifted at 18 decimals, in the wrong direction
# for amountInMax and the addLiquidity desired amounts.
DEFAULT_SLIPPAGE_BPS = 50  # 0.5%


BPS = 10_000  # basis points in one whole


# The widest tolerance any tool accepts. The packages take any value, and 10000 bps sets the
# minimum out to zero -- a trade that accepts getting nothing back, which anyone who can move the
# pool price around it can take almost all of. The cap only bounds that for tokens it counts.
MAX_SLIPPAGE_BPS = 1200  # 12%


def _check_slippage(slippage_bps: int) -> int:
    """Returns `slippage_bps` if it is within 0..MAX_SLIPPAGE_BPS, else refuses it."""
    if not 0 <= slippage_bps <= MAX_SLIPPAGE_BPS:
        raise ToolException(
            f"A slippage of {slippage_bps} bps is outside what this wallet allows: 0 to "
            f"{MAX_SLIPPAGE_BPS} bps ({MAX_SLIPPAGE_BPS / 100:g}%). Nothing was quoted. Ask the user "
            f"for a tolerance within that range."
        )
    return slippage_bps


def _is_listed(chain_id: int, address: str) -> bool:
    """Whether `address` is a token Mitfah lists on `chain_id`."""
    return any(address.lower() == t["address"].lower() for t in _get_listed_tokens(chain_id))


def _token_address(user_id: int, token: str) -> str:
    """Token reference -> checksummed address for an ERC-20 tool: a listed token or one the user
    added in the web app, named by ticker or by address.

    Every ERC-20 and Uniswap tool resolves through here and hands the packages an ADDRESS, so a
    token the user added works everywhere a listed one does -- the packages' own registries only
    know the listed tickers, snapshotted when the toolkit was built.

    An address is accepted only for a token on one of those two lists. Any other contract is
    refused, because a swap or a deposit into a pool is a destination for value that no contact
    check sees: the output comes back to the wallet, but the tokens paid in stay in a pool that
    whoever made it can empty. Adding a token is a web-app action, like adding a contact, so the
    chat can only reach tokens the owner chose. This also shuts out LP tokens by address -- they
    have no price, so the cap would never count them leaving.

    @raises ToolException  If the token is neither listed nor one the user added.
    """
    _, chain_id, _ = load_network_config(user_id)
    try:
        address = _resolve_token(user_id, chain_id, token)
    except ValueError:
        address = None
    if address is None or not (
        _is_listed(chain_id, address) or _get_custom_token(user_id, chain_id, address)
    ):
        raise ToolException(
            f"'{token}' is not a token this wallet knows on this network: it is neither on Mitfah's "
            f"list nor one the user added. Call get_supported_tokens to see both lists. The user can "
            f"add a token by its contract address from the web app (Dashboard -> Balances -> Add token)."
        )
    return address


def _resolve(user_id: int, token: str) -> str:
    """Ticker -> checksummed address, for the address-only langchain-uniswap-v2 tools.

    The native asset ("eth", or "bnb") maps to the chain's wrapped-native token: on a router, native
    ETH/BNB is always routed as its wrapped form, and the *ETH-suffixed router functions wrap/unwrap
    around that same address -- so it also names the pool the native asset trades in.

    @param token  A listed token or one the user added (ticker or address), or "eth"/"bnb".
    """
    if _is_native(token):
        _, chain_id, _ = load_network_config(user_id)
        token = get_native_wrapped_ticker(chain_id)
    return _token_address(user_id, token)


def _is_native(token: str) -> bool:
    """Whether a tool's token argument names the chain's native asset ("eth" everywhere, "bnb" too)."""
    return token.lower() in ("eth", "bnb")


def _unlisted_label(user_id: int, token: str) -> str | None:
    """
    How to name `token` in a quote if Mitfah does NOT list it on this chain, else None.

    An unlisted token -- one the user added -- has no price feed, so the spending cap can never
    count it. The native asset and every listed token return None.
    """
    if _is_native(token):
        return None
    _, chain_id, _ = load_network_config(user_id)
    address = _token_address(user_id, token)
    if _is_listed(chain_id, address):
        return None
    # Never None: _token_address refuses anything neither listed nor added.
    return _get_custom_token(user_id, chain_id, address)["ticker"].upper()


def _token_label(user_id: int, token: str) -> str:
    """
    How to name a token argument to the user: the native asset by its own name (ETH, BNB), any other
    token by its ticker in capitals -- looked up when the model named it by address.
    """
    _, chain_id, _ = load_network_config(user_id)
    if _is_native(token):
        return get_native_asset_ticker(chain_id)
    if not token.lower().startswith("0x"):
        return token.upper()
    address = _token_address(user_id, token)
    for listed in _get_listed_tokens(chain_id):
        if listed["address"].lower() == address.lower():
            return listed["ticker"].upper()
    custom = _get_custom_token(user_id, chain_id, address)
    return custom["ticker"].upper() if custom else address


_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\b")
# "1 of 0x…" or "1 of the native asset", as the packages write an amount of a token.
_AMOUNT_OF = re.compile(r"(\d[\d.,]*) of (0x[0-9a-fA-F]{40}\b|the native asset)")
_NATIVE = re.compile(r"\b(?:the )?native asset\b")


def _with_tickers(user_id: int, text: str) -> str:
    """
    A package's description of a call, with its tokens named the way the user knows them: "Swap 1
    ETH for at least 2390.7 USDC" rather than "Swap 1 of the native asset for at least 2390.7 of
    0x8335…".

    Only a listed token or one the user added is named, from those two lists -- an added token can't
    take a listed ticker or the native asset's name (custom_tokens) -- so the name is still written
    by code, never by the model. Any other address (a recipient, a router) stays as it is.
    """
    _, chain_id, _ = load_network_config(user_id)
    native = get_native_asset_ticker(chain_id)
    tickers = {t["address"].lower(): t["ticker"].upper() for t in _get_listed_tokens(chain_id)}

    def name(token: str) -> str:
        if _NATIVE.fullmatch(token):
            return native
        if token.lower() not in tickers:
            custom = _get_custom_token(user_id, chain_id, token)
            tickers[token.lower()] = custom["ticker"].upper() if custom else token
        return tickers[token.lower()]

    def amount_of(match: re.Match) -> str:
        token = name(match[2])
        # "5 of 0x…" stays as it was for an address nobody named.
        return match[0] if token == match[2] else f"{match[1]} {token}"

    text = _AMOUNT_OF.sub(amount_of, text)
    text = _NATIVE.sub(native, text)
    return _ADDRESS.sub(lambda m: name(m[0]), text)


def _refuse_same_token(user_id: int, a: str, b: str, why: str) -> None:
    """
    Refuses a swap or a pool with the same token on both sides. The native asset and its wrapped
    form count as one: the router trades the native asset as the wrapped token's address.

    @param why  What is impossible, as the end of a sentence ("there is nothing to swap").
    """
    if _resolve(user_id, a) != _resolve(user_id, b):
        return
    hint = ""
    if _is_native(a) and not _is_native(b):
        hint = " To turn the native asset into its wrapped form, use wrap_eth."
    elif _is_native(b) and not _is_native(a):
        hint = " Turning the wrapped token back into the native asset isn't something the assistant can do."
    raise ToolException(
        f"{_token_label(user_id, a)} and {_token_label(user_id, b)} are the same token here, so {why}. "
        f"Nothing was quoted.{hint}"
    )


def _own_limits(user_id: int, *tokens: str) -> list[tuple[str, str, str] | None]:
    """
    For each token, (its name, its limit, what is left of it this period) in whole units if the owner
    limited it in its own units instead of in USD, else None. The native asset can't be limited so.
    """
    functions = load_session_handler(user_id).functions
    erc20s = [None if _is_native(token) else load_ierc20(user_id=user_id, token=token) for token in tokens]
    caps = read_all({
        str(i): functions.getTokenCap(erc20.address).call for i, erc20 in enumerate(erc20s) if erc20 is not None
    })
    limits = []
    for i, token in enumerate(tokens):
        capped, limit, left = caps.get(str(i), (False, 0, 0))
        if not capped:
            limits.append(None)
            continue
        scale = 10 ** erc20s[i].functions.decimals().call()
        limits.append((_token_label(user_id, token), _whole(limit / scale), _whole(left / scale)))
    return limits


def _limit_note(user_id: int, spent: str, received: str | None = None) -> str:
    """
    One sentence for a quote that moves a token with a limit of its own or one Mitfah doesn't list,
    saying how the spending limits treat it. Empty otherwise -- those quotes are unchanged.

    The USD cap meters NET value across the wallet's watched tokens and, if the owner has it counted,
    the native asset. A token the owner limited in its own units is outside that, and an unlisted
    token has no price, so:
      - sending or selling a limited token uses its own limit, however fair the price, and costs
        nothing against the USD cap;
      - sending or selling an unlisted token with no limit costs nothing at all;
      - buying either with a counted token (a watched USDC/USDT/..., or the native asset while it
        counts) counts the FULL amount paid -- the cap sees what left the wallet and cannot value what
        came back -- and doesn't use the token's own limit;
      - buying either with a listed token the wallet doesn't count costs nothing either.

    @param spent     The token leaving the wallet: a ticker, a 0x address, or "eth" for native.
    @param received  For a swap, the token coming back; None for a transfer.
    """
    if received is None:
        spent_limit, received_limit = _own_limits(user_id, spent)[0], None
    else:
        spent_limit, received_limit = _own_limits(user_id, spent, received)
    if spent_limit is not None:
        name, limit, left = spent_limit
        return (
            f"{'Sending' if received is None else 'Selling'} {name} uses the limit the owner set on it "
            f"({left} of {limit} left in this period), not the USD spending limit."
        )
    spent_label = _unlisted_label(user_id, spent)
    if received_limit is not None:
        received_label = received_limit[0]
    else:
        received_label = _unlisted_label(user_id, received) if received is not None else None
    if spent_label is None and received_label is None:
        return ""
    if received is None:
        return (
            f"{spent_label} isn't covered by the spending limit: Mitfah has no price for it, so "
            f"sending it doesn't count toward the limit."
        )
    if spent_label is not None and received_limit is not None:
        return (
            f"Neither {spent_label} nor {received_label} counts toward the USD spending limit, and buying "
            f"{received_label} doesn't use the limit the owner set on it."
        )
    if spent_label is not None and received_label is not None:
        return f"Neither {spent_label} nor {received_label} is covered by the spending limit."
    if spent_label is not None:
        return (
            f"{spent_label} isn't covered by the spending limit, so selling it costs nothing "
            f"against the limit."
        )

    _, chain_id, _ = load_network_config(user_id)
    wallet = load_session_handler(user_id)
    if _is_native(spent):
        payer = get_native_asset_ticker(chain_id)
        # By field name, never position: see contracts.read_spending_config.
        counted = read_spending_config(wallet)["nativeMetered"]
        uncounted = f"{payer} doesn't count on this wallet"
    else:
        payer = spent.upper()
        counted = wallet.functions.isWatched(_token_address(user_id, spent)).call()
        uncounted = f"{payer} isn't on this wallet's counted list"
    if counted:
        why = (
            f"{received_label} has a limit of its own rather than a USD value"
            if received_limit is not None
            else f"Mitfah can't put a price on {received_label}"
        )
        return (
            f"The full amount of {payer} paid counts toward the spending limit: {why}, so nothing is "
            f"taken off for what comes back."
        )
    if received_limit is not None:
        return (
            f"Neither {payer} nor {received_label} counts toward the USD spending limit: {uncounted}, "
            f"and buying {received_label} doesn't use the limit the owner set on it."
        )
    return (
        f"Neither {payer} nor {received_label} counts toward the spending limit: {uncounted} and "
        f"{received_label} has no price."
    )


def _with_note(details: str, note: str) -> str:
    """Appends a _limit_note to a quote's details line."""
    if not note:
        return details
    return f"{details}. {note}" if details else note


def _resolve_contact(user_id: int, name: str, role: str = "recipient", hint: str = "") -> str:
    """Saved-contact name (or "me") -> address, failing usefully when it is neither.

    Every tool that takes a person's name routes through here. Two reasons it is not just a
    db lookup:

      - db.get_contact returns None for an unknown name rather than raising. Passing that None
        onward surfaces as an opaque "must be a string, got NoneType" from deep inside the
        calldata builder, which tells the agent nothing it can act on.
      - It is deliberately NOT address-accepting. The model may only name someone the user
        already saved, so an address injected into the conversation cannot become a
        destination for value (THREAT_MODEL 4.2). That makes writing the contact list the
        privileged step -- and it is why there is no save_contact or delete_contact tool.
        Changing the list requires an authenticated web session (POST / DELETE /api/contacts):
        whoever holds the chat surface can spend the cap on the destinations the OWNER chose,
        and cannot add one. A stolen phone is the case this bounds — the thief reaches the
        agent, but naming themselves as the recipient takes the web credential, not the
        messenger.

    "me" names the wallet itself, matching transferFrom_erc20's documented convention.

    An ENS name (sam.eth) gets no special treatment: it is payable only once the owner has saved it
    as a contact, and then it pays the address the owner signed, not wherever the name points now
    (see ens_names). Resolving it here would let anyone who can type into the chat register a name
    and have it paid.

    @param role  What this name is being used as, for the error message ("sender", "spender", ...).
    @param hint  Optional extra sentence appended to the error, e.g. how to skip the argument.
    @raises ToolException If the name is not a saved contact.
    """
    if name.lower() == "me":
        return load_session_handler(user_id).address

    address = _get_contact(user_id, name)
    if address is None:
        ens = (
            f" The web app can look up '{name}' by its ENS name when the user adds it."
            if "." in name.strip(". ")
            else ""
        )
        raise ToolException(
            f"'{name}' is not a saved contact, so they cannot be used as the {role}. "
            f"Contacts can only be added from the web app, while signed in — you cannot add "
            f"one here, and an address given in this conversation cannot be used instead. "
            f"Tell the user to add the contact there, then ask them to try again.{ens}{hint}"
        )
    if not _is_contact_signed(user_id, name):
        # Saved before the wallet began checking contacts itself (2026-11-26): its old signature
        # wasn't kept, and the wallet pays no one without the owner's signature (PayeeGuard).
        raise ToolException(
            f"'{name}' was saved before your wallet started checking contacts itself, so it needs "
            f"signing once more before it can be paid. Tell the user to open Contacts in the web app "
            f"and tap 'Sign again' next to '{name}', then try again.{hint}"
        )
    return address


def _resolve_recipient(user_id: int, recipient: str | None) -> str | None:
    """Resolve a swap's optional output recipient. None means "the wallet itself".

    This is the one place a swap can send value somewhere other than the wallet, so the
    contact-only rule in _resolve_contact is what bounds it.
    """
    if recipient is None:
        return None
    return _resolve_contact(
        user_id,
        recipient,
        role="swap output recipient",
        hint=" Or omit recipient to receive the output in your own wallet.",
    )


def _destination_note(recipient: str | None) -> str:
    """Result-string suffix naming where a swap's output went, when it wasn't the wallet."""
    if _keeps_output(recipient):
        return ""
    return f", sent directly to: {recipient}"


def _keeps_output(recipient: str | None) -> bool:
    """Whether a swap's output comes back to the wallet rather than going to a contact."""
    return recipient is None or recipient.lower() == "me"


def _addresses_to_tickers(user_id: int, addresses: list) -> list:
    """Best-effort reverse map of token addresses to their supported tickers, falling back to the
    raw address for anything not in the network's token table."""
    reverse = {}
    for ticker in _get_supported_tokens(user_id):
        try:
            reverse[load_ierc20(user_id=user_id, token=ticker).address.lower()] = ticker
        except Exception:
            pass
    return [reverse.get(a.lower(), a) for a in addresses]


# Which way a leg of a transaction moves: out of the wallet, or back into it.
SENT, RECEIVED = "sent", "received"


class _Leg(NamedTuple):
    """One side of a transaction, resolved from the database. See _resolve_legs."""

    token: str               # as it was named
    amount: float            # whole units
    direction: str           # SENT or RECEIVED
    address: str             # ETH_SENTINEL for the native asset
    erc20: Contract | None   # None for the native asset
    unlisted: bool           # a token the user added: it has no price at all
    label: str = ""          # how to name it to the user (_token_label); the token itself if unset


def _resolve_legs(user_id: int, legs: list[tuple[str, float, str]]) -> list[_Leg]:
    """
    The database half of checking a transaction: each (token, amount, direction) resolved to what
    _wallet_checks has to read. Run on the caller's thread, before any read starts (see parallel.py).

    @raises ToolException  If a token is neither listed nor added -- which would otherwise be waved
                           through as unmetered, and so "costs nothing".
    """
    resolved = []
    for token, amount, direction in legs:
        label = _token_label(user_id, token)
        if _is_native(token):
            resolved.append(_Leg(token, amount, direction, ETH_SENTINEL, None, False, label))
            continue
        _token_address(user_id, token)
        erc20 = load_ierc20(user_id=user_id, token=token)
        unlisted = _unlisted_label(user_id, erc20.address) is not None
        resolved.append(_Leg(token, amount, direction, erc20.address, erc20, unlisted, label))
    return resolved


def _whole(amount: float) -> str:
    """An amount in whole units for a message: 1,000,000 or 0.892005, never 1e+06."""
    return f"{amount:,.6f}".rstrip("0").rstrip(".")
