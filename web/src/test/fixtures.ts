import type { Passkey, Position, SessionBlock, SessionKey, WalletState } from '../api/types'

export const SEPOLIA = 11155111
export const USDC = '0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8'
export const WETH = '0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14'
/** The test account's passkey on its iPhone: how the wallet names it, and its WebAuthn credential id. */
export const PASSKEY_ID = `0x${'ab'.repeat(32)}`
export const PASSKEY_CREDENTIAL = 'aVBob25lLXBhc3NrZXk'
/** A key the wallet trusts that Mitfah doesn't know: granted outside the app. */
export const FOREIGN_KEY_ID = `0x${'cd'.repeat(32)}`

export type SessionFixture = 'on' | 'expiring' | 'expired' | 'off' | 'foreign'

/**
 * One key of GET /api/wallet's `session`: the iPhone's passkey with 20 days left, unless overridden.
 * The derived fields follow `valid_until` as the API works them out.
 */
export function makeSessionKey(overrides: Partial<SessionKey> = {}, nowSecs = Math.floor(Date.now() / 1000)): SessionKey {
  const validUntil = overrides.valid_until ?? nowSecs + 20 * 86_400
  return {
    key_id: PASSKEY_ID,
    device_name: 'iPhone',
    credential_id: PASSKEY_CREDENTIAL,
    valid_until: validUntil,
    expires_in_secs: Math.max(validUntil - nowSecs, 0),
    active: nowSecs <= validUntil,
    // The API's rule: under three days left, which a key that already ran out is too.
    needs_renewal: validUntil - nowSecs < 3 * 86_400,
    ...overrides,
  }
}

/** The `session` block around `keys`, with the contract's limits. */
export function makeSessionBlock(keys: SessionKey[]): SessionBlock {
  return { max_keys: 5, max_ttl_secs: 90 * 86_400, renewal_warning_secs: 3 * 86_400, keys }
}

/**
 * The `session` block as GET /api/wallet reports each state (see lib/assistant.ts): the iPhone's
 * passkey with 20 days left (`on`), 2 (`expiring`), or run out a day before `nowSecs` (`expired`);
 * no key at all (`off`); or only a key added outside Mitfah (`foreign`).
 */
export function makeSession(state: SessionFixture = 'on', nowSecs = Math.floor(Date.now() / 1000)): SessionBlock {
  if (state === 'off') return makeSessionBlock([])
  if (state === 'foreign') {
    return makeSessionBlock([makeSessionKey({ key_id: FOREIGN_KEY_ID, device_name: null, credential_id: null }, nowSecs)])
  }
  const days = { on: 20, expiring: 2, expired: -1 }[state]
  return makeSessionBlock([makeSessionKey({ valid_until: nowSecs + days * 86_400 }, nowSecs)])
}

/** One of the account's passkeys, as GET /api/passkeys lists it: the iPhone's, unless overridden. */
export function makePasskey(overrides: Partial<Passkey> = {}): Passkey {
  return {
    key_id: PASSKEY_ID,
    credential_id: PASSKEY_CREDENTIAL,
    device_name: 'iPhone',
    created_at: 1_790_000_000,
    ...overrides,
  }
}

/**
 * A healthy wallet, as GET /api/wallet/{chain_id} returns it: active, the assistant on, $40 of a
 * $100 daily limit spent in a period that started an hour before `nowSecs`.
 */
export function makeWalletState(
  overrides: Partial<WalletState> = {},
  nowSecs = Math.floor(Date.now() / 1000),
): WalletState {
  return {
    chain_id: SEPOLIA,
    chain_name: 'sepolia-fork',
    address: '0x2222222222222222222222222222222222222222',
    owner: '0x1111111111111111111111111111111111111111',
    is_owner: true,
    paused: false,
    spending: {
      hook_installed: true,
      daily_limit_usd: 100,
      spent_usd: 40,
      remaining_usd: 60,
      window_hours: 24,
      window_start: nowSecs - 3_600,
      watched_tokens: [{ ticker: 'usdc', address: USDC }],
    },
    session: makeSession('on', nowSecs),
    limits: {
      max_op_gas_cost_wei: '10000000000000000',
      session_gas: { budget_wei: '50000000000000000', remaining_wei: '50000000000000000', refills_at: null },
      allowlist_enabled: false,
      trusted_spenders: [],
    },
    balances: [
      { ticker: 'eth', address: null, native: true, decimals: 18, raw: '1500000000000000000', amount: 1.5 },
      { ticker: 'usdc', address: USDC, native: false, decimals: 6, raw: '25000000', amount: 25 },
      { ticker: 'weth', address: WETH, native: false, always_counted: true, decimals: 18, raw: '0', amount: 0 },
    ],
    positions: [],
    ...overrides,
  }
}

/**
 * A Uniswap V3 position as GET /api/wallet lists it: ETH/USDC in the 0.05% pool, in range between
 * about 2,850 and 3,150 USDC per ETH, holding 1.2 ETH and 3,400 USDC, with 0.004 ETH and 12.5 USDC
 * of fees unclaimed. The bounds aren't round, as a range's bounds snap to the pool's ticks.
 */
export function makePosition(overrides: Partial<Position> = {}): Position {
  return {
    id: 101,
    pair: 'eth/usdc',
    base: 'eth',
    quote: 'usdc',
    fee_pct: 0.05,
    min_price: 2850.4321,
    max_price: 3150.1234,
    full_range: false,
    current_price: 3020.2468,
    in_range: true,
    amounts: [
      { ticker: 'eth', decimals: 18, raw: '1200000000000000000' },
      { ticker: 'usdc', decimals: 6, raw: '3400000000' },
    ],
    fees: [
      { ticker: 'eth', decimals: 18, raw: '4000000000000000' },
      { ticker: 'usdc', decimals: 6, raw: '12500000' },
    ],
    ...overrides,
  }
}
