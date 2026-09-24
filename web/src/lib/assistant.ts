import type { WalletState } from '../api/types'

/** How long a key lasts when the owner turns the assistant on or renews it. The wallet allows 90. */
export const ASSISTANT_KEY_DAYS = 30
export const ASSISTANT_KEY_TTL_SECS = ASSISTANT_KEY_DAYS * 86_400

/**
 * Where the assistant stands with the wallet:
 * - `on`: the wallet trusts Mitfah's key and it has time left.
 * - `expiring`: as `on`, but it runs out within a few days, so it's worth renewing now.
 * - `expired`: the wallet still names Mitfah's key, but its time is up.
 * - `off`: the wallet trusts no key.
 * - `foreign`: the wallet trusts a key Mitfah doesn't hold (granted outside the app), so the
 *   assistant can't sign; turning it on replaces that key.
 */
export type AssistantStatus = 'on' | 'expiring' | 'expired' | 'off' | 'foreign'

export function assistantStatus(session: WalletState['session']): AssistantStatus {
  if (session.wallet_key === null) return 'off'
  if (!session.is_app_key) return 'foreign'
  // The wallet names Mitfah's key, so the only thing that can stop it is its deadline.
  if (!session.active) return 'expired'
  return session.needs_renewal ? 'expiring' : 'on'
}
