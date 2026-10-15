import type { Position, WalletState } from '../api/types'

export const SEPOLIA = 11155111
export const USDC = '0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8'
export const WETH = '0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14'
/** The assistant's key, as Mitfah holds it. */
export const APP_KEY = '0x5555555555555555555555555555555555555555'
/** A key the wallet trusts that Mitfah doesn't hold. */
export const FOREIGN_KEY = '0x6666666666666666666666666666666666666666'

export type SessionFixture = 'on' | 'expiring' | 'expired' | 'off' | 'foreign'

/**
 * The `session` block as GET /api/wallet reports each state (see lib/assistant.ts): `on` has 20
 * days left, `expiring` 2 and `expired` ran out a day before `nowSecs`. `off` is after a
 * revocation, which clears the wallet's key and its deadline and makes the app forget its copy.
 */
export function makeSession(
  state: SessionFixture = 'on',
  nowSecs = Math.floor(Date.now() / 1000),
): WalletState['session'] {
  if (state === 'off') {
    return { key: null, wallet_key: null, is_app_key: false, active: false, expires_at: null, expires_in_secs: 0, needs_renewal: false }
  }
  const expiresAt = nowSecs + { on: 20, expiring: 2, expired: -1, foreign: 20 }[state] * 86_400
  return {
    key: APP_KEY,
    wallet_key: state === 'foreign' ? FOREIGN_KEY : APP_KEY,
    is_app_key: state !== 'foreign',
    active: state === 'on' || state === 'expiring',
    expires_at: expiresAt,
    expires_in_secs: Math.max(expiresAt - nowSecs, 0),
    // The API's rule: under three days left, which a key that already ran out is too.
    needs_renewal: expiresAt - nowSecs < 3 * 86_400,
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
