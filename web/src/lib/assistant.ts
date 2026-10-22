import type { SessionBlock, SessionKey } from '../api/types'

/** How long a device may approve payments when the owner adds or renews it. The wallet allows 90. */
export const ASSISTANT_KEY_DAYS = 30
export const ASSISTANT_KEY_TTL_SECS = ASSISTANT_KEY_DAYS * 86_400

/**
 * Where the assistant stands with the wallet, from the devices it trusts. A payment is sent only once
 * a passkey the wallet trusts approves it, and Mitfah can ask only the passkeys it knows:
 * - `on`: at least one of the user's devices can approve payments.
 * - `expiring`: as `on`, but every such device runs out within a few days, so it's worth renewing.
 * - `expired`: the wallet still lists the user's devices, but their time is up.
 * - `off`: the wallet trusts no device.
 * - `foreign`: it trusts only keys added outside Mitfah, which Mitfah can't ask to approve anything.
 */
export type AssistantStatus = 'on' | 'expiring' | 'expired' | 'off' | 'foreign'

/** A key Mitfah knows: a passkey on one of the user's devices. */
const known = (key: SessionKey) => key.credential_id !== null

export function assistantStatus(session: SessionBlock): AssistantStatus {
  const mine = session.keys.filter(known)
  const live = mine.filter(key => key.active)
  if (live.length > 0) return live.every(key => key.needs_renewal) ? 'expiring' : 'on'
  if (mine.length > 0) return 'expired'
  return session.keys.some(key => key.active) ? 'foreign' : 'off'
}

/**
 * When the assistant stops being able to send (ms), unless a device is renewed: the last deadline
 * among the user's devices that still work, or, once none do, among those that ran out. Null when
 * the wallet lists none of the user's devices.
 */
export function assistantDeadline(session: SessionBlock): number | null {
  const mine = session.keys.filter(known)
  const live = mine.filter(key => key.active)
  const pool = live.length > 0 ? live : mine
  return pool.length > 0 ? Math.max(...pool.map(key => key.valid_until)) * 1_000 : null
}
