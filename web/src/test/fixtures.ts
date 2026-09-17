import type { WalletState } from '../api/types'

export const SEPOLIA = 11155111
export const USDC = '0x94a9D9AC8a22534E3FaCa9F4e7F2E2cf85d5E4C8'
export const WETH = '0xfFf9976782d46CC05630D1f6eBAb18b2324d6B14'

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
    session: { key: '0x5555555555555555555555555555555555555555', active: true },
    limits: { max_op_gas_cost_wei: '10000000000000000', allowlist_enabled: false, trusted_spenders: [] },
    balances: [
      { ticker: 'eth', address: null, native: true, decimals: 18, raw: '1500000000000000000', amount: 1.5 },
      { ticker: 'usdc', address: USDC, native: false, decimals: 6, raw: '25000000', amount: 25 },
      { ticker: 'weth', address: WETH, native: false, decimals: 18, raw: '0', amount: 0 },
    ],
    ...overrides,
  }
}
