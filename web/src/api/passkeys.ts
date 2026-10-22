import { apiFetch } from './client'
import type { Passkey, PasskeyRegistrationJSON, RegisteredPasskey } from './types'

/**
 * Asks the server for a fresh, single-use challenge to make a passkey with. The options name this
 * site, the account's own user handle, ES256 only, and the account's passkeys to leave out (a device
 * gets one passkey). Asked for before the tap, so the tap can open the browser's prompt at once.
 */
export async function fetchRegistrationOptions(signal?: AbortSignal) {
  const { options } = await apiFetch<{ options: PublicKeyCredentialCreationOptionsJSON }>(
    '/api/passkeys/register/options',
    { method: 'POST', signal },
  )
  return options
}

/**
 * Saves a passkey the browser just made, under the name the user gave this device (1-40
 * characters). The server answers 400 when it can't verify the passkey or it isn't ES256, and 409
 * when it is already saved.
 */
export function registerPasskey(credential: PasskeyRegistrationJSON, deviceName: string) {
  return apiFetch<RegisteredPasskey>('/api/passkeys/register', {
    method: 'POST',
    body: { credential, device_name: deviceName },
  })
}

/** The account's passkeys, on whichever device and wallet. */
export async function fetchPasskeys() {
  const { passkeys } = await apiFetch<{ passkeys: Passkey[] }>('/api/passkeys')
  return passkeys
}

/**
 * Forgets one of the account's passkeys. The server answers 409 while any of the account's wallets
 * still approves it (revoke it there first), and 503 when it couldn't read a wallet to tell.
 */
export function deletePasskey(keyId: string) {
  return apiFetch<void>(`/api/passkeys/${encodeURIComponent(keyId)}`, { method: 'DELETE' })
}

/** Renames a device. The name shows wherever its passkey does, on every network. */
export function renamePasskey(keyId: string, deviceName: string) {
  return apiFetch<Passkey>(`/api/passkeys/${encodeURIComponent(keyId)}`, {
    method: 'PATCH',
    body: { device_name: deviceName },
  })
}
