/**
 * Passkeys: how a device approves the assistant's payments. Each device makes one (WebAuthn, ES256 on
 * P-256) and the wallet's owner lets the wallet trust it; from then on every payment the assistant
 * prepares is sent only once a passkey signs it, with Face ID, a fingerprint or the device's PIN.
 * The server holds no key at all, only each passkey's public key.
 *
 * Safari opens the passkey prompt only for a call made inside the tap that asked for it, so the two
 * calls here that open it — createPasskey and signWithPasskey — reach navigator.credentials before
 * returning, with no await ahead of it. Whatever the server has to say first (the options, the
 * challenge) is fetched before the tap.
 *
 * Binary fields travel as base64url without padding, in the JSON shapes the WebAuthn spec names
 * (RegistrationResponseJSON, AuthenticationResponseJSON), which the server's py_webauthn reads.
 */
import type { PasskeyAssertionJSON, PasskeyRegistrationJSON, SignStart } from '../api/types'

// ── Errors ────────────────────────────────────────────────────────────────────

/**
 * - `unsupported`: this browser or device can't use passkeys.
 * - `cancelled`: the prompt was closed (or timed out); nothing happened.
 * - `no-passkey`: the prompt closed and this device holds none of the passkeys that were asked for,
 *   as far as this browser knows. Nothing happened.
 * - `exists`: making a passkey, but this device already holds one of the account's.
 * - `failed`: anything else the browser refused.
 */
export type PasskeyErrorKind = 'unsupported' | 'cancelled' | 'no-passkey' | 'exists' | 'failed'

/** A passkey prompt that didn't give a passkey or a signature. Its message is written for the user. */
export class PasskeyError extends Error {
  readonly kind: PasskeyErrorKind

  constructor(kind: PasskeyErrorKind, message: string, options?: { cause?: unknown }) {
    super(message, options)
    this.name = 'PasskeyError'
    this.kind = kind
  }
}

export function isPasskeyError(error: unknown, kind?: PasskeyErrorKind): error is PasskeyError {
  return error instanceof PasskeyError && (kind === undefined || error.kind === kind)
}

const UNSUPPORTED = "This browser can't use passkeys. Open this page in Safari or Chrome."

/** What the browser's refusal means here, in plain words. */
function asPasskeyError(error: unknown, task: 'create' | 'sign', asked: string[] = []): PasskeyError {
  if (error instanceof PasskeyError) return error
  const name = (error as { name?: unknown } | null)?.name
  switch (name) {
    case 'NotAllowedError':
    case 'AbortError':
      if (task === 'create') return new PasskeyError('cancelled', 'Cancelled. No passkey was made.', { cause: error })
      // The same refusal covers a closed prompt and a device with none of these passkeys: say which is
      // likelier, from what this browser has made or used before.
      return asked.some(isOnThisDevice)
        ? new PasskeyError('cancelled', 'Cancelled. Nothing was sent.', { cause: error })
        : new PasskeyError(
            'no-passkey',
            'Nothing was sent. If this device has no approved passkey, confirm on a device that has one, or add this one in Controls.',
            { cause: error },
          )
    case 'InvalidStateError':
      if (task === 'create') {
        return new PasskeyError('exists', 'This device already has a Mitfah passkey.', { cause: error })
      }
      break
    case 'NotSupportedError':
      return new PasskeyError('unsupported', "This device can't make the kind of passkey Mitfah uses.", { cause: error })
    case 'ConstraintError':
      return new PasskeyError(
        'unsupported',
        'This device needs a screen lock (Face ID, a fingerprint or a PIN) before it can use a passkey.',
        { cause: error },
      )
    case 'SecurityError':
      return new PasskeyError('failed', "Passkeys for Mitfah don't work on this page's address.", { cause: error })
  }
  return new PasskeyError(
    'failed',
    task === 'create' ? "Your browser couldn't make a passkey. Try again." : "Your passkey didn't answer. Try again.",
    { cause: error },
  )
}

/** Whether this browser has WebAuthn at all. A device may still lack a screen lock to use it with. */
export function passkeysSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.PublicKeyCredential === 'function' &&
    typeof navigator.credentials?.create === 'function' &&
    typeof navigator.credentials?.get === 'function'
  )
}

// ── Base64url ─────────────────────────────────────────────────────────────────

export function toBase64Url(bytes: ArrayBuffer | Uint8Array): string {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  let binary = ''
  for (const byte of view) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** @throws Error for text that isn't base64url. */
export function fromBase64Url(text: string): ArrayBuffer {
  if (!/^[A-Za-z0-9_-]*$/.test(text)) throw new Error('Not base64url.')
  const base64 = text.replace(/-/g, '+').replace(/_/g, '/')
  const binary = atob(base64 + '='.repeat((4 - (base64.length % 4)) % 4))
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
  return bytes.buffer
}

// ── This device's passkeys ────────────────────────────────────────────────────

/**
 * Credential ids this browser has made, or signed with from its own authenticator (a passkey synced
 * from another of the user's devices counts: it is on this one too). Only a hint, to mark "This
 * device" and to guess why a prompt closed: site data can be cleared, and a passkey on a phone used
 * through a QR code is deliberately not counted.
 */
const STORE_KEY = 'mitfah-passkeys'
const STORE_LIMIT = 20
const listeners = new Set<() => void>()

/** The stored list as it is (a string, so a React store can compare it); '' when there is none. */
export function devicePasskeysSnapshot(): string {
  try {
    return localStorage.getItem(STORE_KEY) ?? ''
  } catch {
    return ''
  }
}

/** The credential ids in a snapshot. */
export function parseDevicePasskeys(snapshot: string): string[] {
  try {
    const parsed: unknown = JSON.parse(snapshot || '[]')
    return Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === 'string') : []
  } catch {
    return []
  }
}

/** Calls `listener` whenever the list changes, in this tab or another. */
export function subscribeDevicePasskeys(listener: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORE_KEY || event.key === null) listener()
  }
  listeners.add(listener)
  window.addEventListener('storage', onStorage)
  return () => {
    listeners.delete(listener)
    window.removeEventListener('storage', onStorage)
  }
}

export function rememberPasskey(credentialId: string) {
  try {
    const ids = parseDevicePasskeys(devicePasskeysSnapshot())
    const next = [credentialId, ...ids.filter(id => id !== credentialId)].slice(0, STORE_LIMIT)
    localStorage.setItem(STORE_KEY, JSON.stringify(next))
  } catch {
    // Storage blocked: the passkey still works; this browser just won't know it is the one here.
  }
  listeners.forEach(listener => listener())
}

export function isOnThisDevice(credentialId: string | null | undefined): boolean {
  return !!credentialId && parseDevicePasskeys(devicePasskeysSnapshot()).includes(credentialId)
}

/**
 * Checked by shape, not `instanceof`: a password manager that answers for the browser may hand back
 * an object of its own.
 */
function isPublicKeyCredential(value: Credential | null): value is PublicKeyCredential {
  return !!value && typeof value === 'object' && 'rawId' in value && 'response' in value
}

/** Remembered only when the browser's own authenticator answered, not a phone or a security key. */
function rememberIfLocal(credential: PublicKeyCredential) {
  if (credential.authenticatorAttachment !== 'cross-platform') rememberPasskey(credential.id)
}

// ── Making a passkey ──────────────────────────────────────────────────────────

/**
 * A ceremony left open (a prompt the browser never answered) blocks the next one, so each new one
 * ends the last.
 */
let ceremony: AbortController | null = null

function nextCeremony(): AbortSignal | undefined {
  if (typeof AbortController === 'undefined') return undefined
  ceremony?.abort()
  ceremony = new AbortController()
  return ceremony.signal
}

function toRegistrationJSON(credential: PublicKeyCredential): PasskeyRegistrationJSON {
  const response = credential.response as AuthenticatorAttestationResponse
  // Missing from older browsers; the server reads the key from the attestation object anyway.
  const extra = response as Partial<AuthenticatorAttestationResponse>
  const optional = <T>(read: () => T | undefined): T | undefined => {
    try {
      return read()
    } catch {
      return undefined
    }
  }
  const publicKey = optional(() => extra.getPublicKey?.call(response) ?? undefined)
  const authenticatorData = optional(() => extra.getAuthenticatorData?.call(response))
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: 'public-key',
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    clientExtensionResults: (credential.getClientExtensionResults?.() ?? {}) as Record<string, unknown>,
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      attestationObject: toBase64Url(response.attestationObject),
      transports: optional(() => extra.getTransports?.call(response)),
      authenticatorData: authenticatorData && toBase64Url(authenticatorData),
      publicKey: publicKey && toBase64Url(publicKey),
      publicKeyAlgorithm: optional(() => extra.getPublicKeyAlgorithm?.call(response)),
    },
  }
}

/**
 * Opens the browser's prompt to make a passkey for this site. Call it straight from the tap, with
 * options fetched beforehand (fetchRegistrationOptions).
 *
 * @throws PasskeyError (the promise rejects): `unsupported`, `cancelled`, `exists` or `failed`.
 */
export function createPasskey(options: PublicKeyCredentialCreationOptionsJSON): Promise<PasskeyRegistrationJSON> {
  let request: Promise<Credential | null>
  try {
    if (!passkeysSupported()) throw new PasskeyError('unsupported', UNSUPPORTED)
    const publicKey: PublicKeyCredentialCreationOptions = {
      rp: options.rp,
      user: { ...options.user, id: fromBase64Url(options.user.id) },
      challenge: fromBase64Url(options.challenge),
      pubKeyCredParams: options.pubKeyCredParams,
      timeout: options.timeout,
      excludeCredentials: options.excludeCredentials?.map(credential => ({
        type: 'public-key',
        id: fromBase64Url(credential.id),
        transports: credential.transports as AuthenticatorTransport[] | undefined,
      })),
      authenticatorSelection: options.authenticatorSelection,
      attestation: options.attestation as AttestationConveyancePreference | undefined,
      extensions: options.extensions as AuthenticationExtensionsClientInputs | undefined,
    }
    request = navigator.credentials.create({ publicKey, signal: nextCeremony() })
  } catch (error) {
    return Promise.reject(asPasskeyError(error, 'create'))
  }
  return request.then(
    credential => {
      if (!isPublicKeyCredential(credential)) throw new PasskeyError('failed', "Your browser didn't make a passkey.")
      rememberIfLocal(credential)
      return toRegistrationJSON(credential)
    },
    error => {
      throw asPasskeyError(error, 'create')
    },
  )
}

// ── Signing ───────────────────────────────────────────────────────────────────

function toAssertionJSON(credential: PublicKeyCredential): PasskeyAssertionJSON {
  const response = credential.response as AuthenticatorAssertionResponse
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: 'public-key',
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
    clientExtensionResults: (credential.getClientExtensionResults?.() ?? {}) as Record<string, unknown>,
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      authenticatorData: toBase64Url(response.authenticatorData),
      signature: toBase64Url(response.signature),
      userHandle: response.userHandle ? toBase64Url(response.userHandle) : undefined,
    },
  }
}

/**
 * Opens the passkey prompt for a confirmation (POST /api/sign/start's answer): only the passkeys this
 * wallet trusts, and the device must check it is the user (Face ID, fingerprint or PIN), not just
 * that someone is there. Call it straight from the tap.
 *
 * @throws PasskeyError (the promise rejects): `unsupported`, `cancelled`, `no-passkey` or `failed`.
 */
export function signWithPasskey(start: SignStart): Promise<PasskeyAssertionJSON> {
  const asked = start.allow_credentials.map(credential => credential.id)
  let request: Promise<Credential | null>
  try {
    if (!passkeysSupported()) throw new PasskeyError('unsupported', UNSUPPORTED)
    request = navigator.credentials.get({
      publicKey: {
        challenge: fromBase64Url(start.challenge),
        rpId: start.rp_id,
        allowCredentials: start.allow_credentials.map(credential => ({
          type: 'public-key',
          id: fromBase64Url(credential.id),
          transports: credential.transports as AuthenticatorTransport[] | undefined,
        })),
        userVerification: start.user_verification,
        timeout: start.timeout_ms,
      },
      signal: nextCeremony(),
    })
  } catch (error) {
    return Promise.reject(asPasskeyError(error, 'sign', asked))
  }
  return request.then(
    credential => {
      if (!isPublicKeyCredential(credential)) throw new PasskeyError('failed', "Your passkey didn't answer. Try again.")
      rememberIfLocal(credential)
      return toAssertionJSON(credential)
    },
    error => {
      throw asPasskeyError(error, 'sign', asked)
    },
  )
}

/**
 * Finds which of `credentialIds` this device holds, by asking it to sign a throwaway challenge. For a
 * device that refused to make a passkey because it already has one of the account's (synced from
 * another device, or made before this browser's data was cleared). Call it straight from a tap.
 *
 * @return  The credential id the device answered with.
 */
export function findDevicePasskey(credentialIds: string[]): Promise<string> {
  let request: Promise<Credential | null>
  try {
    if (!passkeysSupported()) throw new PasskeyError('unsupported', UNSUPPORTED)
    request = navigator.credentials.get({
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        allowCredentials: credentialIds.map(id => ({ type: 'public-key', id: fromBase64Url(id) })),
        userVerification: 'preferred',
      },
      signal: nextCeremony(),
    })
  } catch (error) {
    return Promise.reject(asPasskeyError(error, 'sign', credentialIds))
  }
  return request.then(
    credential => {
      if (!isPublicKeyCredential(credential)) throw new PasskeyError('failed', "Your passkey didn't answer. Try again.")
      rememberIfLocal(credential)
      return credential.id
    },
    error => {
      throw asPasskeyError(error, 'sign', credentialIds)
    },
  )
}

// ── Naming a device ───────────────────────────────────────────────────────────

export const DEVICE_NAME_MAX = 40

/**
 * A first guess at what to call this device, from the browser's user agent: "iPhone", "Mac", …
 * The user can change it before the passkey is made, and rename it later.
 *
 * @param touchPoints  iPadOS reports itself as a Mac; only a Mac with a touch screen is an iPad.
 */
export function defaultDeviceName(
  userAgent: string = typeof navigator === 'undefined' ? '' : navigator.userAgent,
  touchPoints: number = typeof navigator === 'undefined' ? 0 : (navigator.maxTouchPoints ?? 0),
): string {
  if (/iPhone|iPod/.test(userAgent)) return 'iPhone'
  if (/iPad/.test(userAgent) || (/Macintosh/.test(userAgent) && touchPoints > 1)) return 'iPad'
  if (/Android/.test(userAgent)) return /Mobile/.test(userAgent) ? 'Android phone' : 'Android tablet'
  if (/Macintosh|Mac OS X/.test(userAgent)) return 'Mac'
  if (/CrOS/.test(userAgent)) return 'Chromebook'
  if (/Windows/.test(userAgent)) return 'Windows PC'
  if (/Linux/.test(userAgent)) return 'Linux computer'
  return 'My device'
}

/** Why a device name can't be used, or null when it can. The server allows 1-40 characters. */
export function deviceNameProblem(name: string): string | null {
  const trimmed = name.trim()
  if (trimmed === '') return 'Give this device a name, such as “iPhone”.'
  if (trimmed.length > DEVICE_NAME_MAX) return `Use at most ${DEVICE_NAME_MAX} characters.`
  return null
}
