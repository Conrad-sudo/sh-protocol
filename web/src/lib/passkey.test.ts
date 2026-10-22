import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SignStart } from '../api/types'
import { b64url, installPasskeys, madeCredential, refused, signedCredential } from '../test/webauthn'
import {
  createPasskey,
  defaultDeviceName,
  deviceNameProblem,
  findDevicePasskey,
  fromBase64Url,
  isOnThisDevice,
  PasskeyError,
  passkeysSupported,
  rememberPasskey,
  signWithPasskey,
  toBase64Url,
} from './passkey'

const CREDENTIAL = b64url('credential-one')
const OTHER = b64url('credential-two')

/** POST /api/passkeys/register/options, as the spec has the server write them. */
const OPTIONS: PublicKeyCredentialCreationOptionsJSON = {
  rp: { id: 'localhost', name: 'Mitfah' },
  user: { id: b64url('user-handle-1234'), name: 'sam', displayName: 'sam' },
  challenge: b64url('registration challenge 32 bytes!'),
  pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  authenticatorSelection: { residentKey: 'preferred', userVerification: 'required' },
  attestation: 'none',
  excludeCredentials: [{ type: 'public-key', id: OTHER }],
  timeout: 60_000,
}

/** POST /api/sign/start's answer, trimmed to what the prompt needs. */
function start(overrides: Partial<SignStart> = {}): SignStart {
  return {
    sign_id: 'sign-1',
    challenge: b64url('the user operation hash, 32 b!!!'),
    rp_id: 'localhost',
    allow_credentials: [{ type: 'public-key', id: CREDENTIAL }],
    user_verification: 'required',
    timeout_ms: 60_000,
    expires_at: Math.floor(Date.now() / 1000) + 300,
    summary: { action: 'Transfer 5 USDC to sam', network: 'Sepolia', chain_id: 11155111, cost: { fees: '$0.35' } },
    ...overrides,
  }
}

const bytesOf = (buffer: BufferSource | undefined) => new TextDecoder().decode(buffer as ArrayBuffer)

describe('base64url', () => {
  it('round-trips every byte, without padding or + and /', () => {
    const all = new Uint8Array(256).map((_, i) => i)
    const encoded = toBase64Url(all)
    expect(encoded).not.toMatch(/[+/=]/)
    expect(new Uint8Array(fromBase64Url(encoded))).toEqual(all)
    expect(toBase64Url(new Uint8Array([251, 255]))).toBe('-_8')
  })

  it('refuses text that is not base64url', () => {
    expect(() => fromBase64Url('a+b/')).toThrow()
  })
})

describe('passkeys', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('opens the prompt to make one inside the call, then answers in base64url', async () => {
    const browser = installPasskeys()
    browser.create.mockResolvedValue(madeCredential(CREDENTIAL))

    const made = createPasskey(OPTIONS)
    // Before any await: Safari only opens the prompt for a call made inside the tap.
    expect(browser.create).toHaveBeenCalledTimes(1)
    const json = await made

    const { publicKey } = browser.create.mock.calls[0][0]!
    expect(bytesOf(publicKey!.challenge)).toBe('registration challenge 32 bytes!')
    expect(bytesOf(publicKey!.user.id)).toBe('user-handle-1234')
    expect(publicKey!.excludeCredentials!.map(c => bytesOf(c.id))).toEqual(['credential-two'])
    expect(publicKey!.pubKeyCredParams).toEqual([{ type: 'public-key', alg: -7 }])
    expect(publicKey!.authenticatorSelection).toEqual({ residentKey: 'preferred', userVerification: 'required' })

    expect(json).toEqual({
      id: CREDENTIAL,
      rawId: CREDENTIAL,
      type: 'public-key',
      authenticatorAttachment: 'platform',
      clientExtensionResults: {},
      response: {
        clientDataJSON: b64url('{"type":"webauthn.create"}'),
        attestationObject: b64url('attestation'),
        transports: ['internal', 'hybrid'],
        authenticatorData: b64url('authenticator data'),
        publicKey: b64url('public key'),
        publicKeyAlgorithm: -7,
      },
    })
    // Made by this browser's own authenticator, so the Devices card can say "This device".
    expect(isOnThisDevice(CREDENTIAL)).toBe(true)
  })

  it("doesn't count a passkey made on a phone through a QR code as this device's", async () => {
    const browser = installPasskeys()
    browser.create.mockResolvedValue(madeCredential(CREDENTIAL, { attachment: 'cross-platform' }))
    await createPasskey(OPTIONS)
    expect(isOnThisDevice(CREDENTIAL)).toBe(false)
  })

  it('says plainly why no passkey was made', async () => {
    const browser = installPasskeys()
    browser.create.mockRejectedValueOnce(refused('NotAllowedError'))
    await expect(createPasskey(OPTIONS)).rejects.toMatchObject({ kind: 'cancelled', message: 'Cancelled. No passkey was made.' })

    browser.create.mockRejectedValueOnce(refused('InvalidStateError'))
    await expect(createPasskey(OPTIONS)).rejects.toMatchObject({ kind: 'exists' })

    browser.create.mockRejectedValueOnce(refused('ConstraintError'))
    await expect(createPasskey(OPTIONS)).rejects.toMatchObject({ kind: 'unsupported', message: /needs a screen lock/ })

    browser.create.mockRejectedValueOnce(new TypeError('Bad options'))
    await expect(createPasskey(OPTIONS)).rejects.toBeInstanceOf(PasskeyError)
  })

  it("says so when the browser can't use passkeys at all", async () => {
    expect(passkeysSupported()).toBe(false)
    await expect(createPasskey(OPTIONS)).rejects.toMatchObject({ kind: 'unsupported', message: /can't use passkeys/ })
    await expect(signWithPasskey(start())).rejects.toMatchObject({ kind: 'unsupported' })
  })

  it('signs a confirmation with only the allowed passkeys, asking the device to verify the user', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(CREDENTIAL))

    const signing = signWithPasskey(start())
    expect(browser.get).toHaveBeenCalledTimes(1)
    const json = await signing

    const { publicKey } = browser.get.mock.calls[0][0]!
    expect(bytesOf(publicKey!.challenge)).toBe('the user operation hash, 32 b!!!')
    expect(publicKey!.rpId).toBe('localhost')
    expect(publicKey!.userVerification).toBe('required')
    expect(publicKey!.timeout).toBe(60_000)
    expect(publicKey!.allowCredentials!.map(c => bytesOf(c.id))).toEqual(['credential-one'])
    expect(json).toEqual({
      id: CREDENTIAL,
      rawId: CREDENTIAL,
      type: 'public-key',
      authenticatorAttachment: 'platform',
      clientExtensionResults: {},
      response: {
        clientDataJSON: b64url('{"type":"webauthn.get"}'),
        authenticatorData: b64url('authenticator data'),
        signature: b64url('signature'),
        userHandle: b64url('user handle'),
      },
    })
    // A passkey synced from another device signed here: it is on this one too.
    expect(isOnThisDevice(CREDENTIAL)).toBe(true)
  })

  it('tells a closed prompt from a device with none of the passkeys, as well as it can', async () => {
    const browser = installPasskeys()
    browser.get.mockRejectedValue(refused())
    // This browser has never made or used any of them.
    await expect(signWithPasskey(start())).rejects.toMatchObject({
      kind: 'no-passkey',
      message: /If this device has no approved passkey/,
    })
    rememberPasskey(CREDENTIAL)
    await expect(signWithPasskey(start())).rejects.toMatchObject({ kind: 'cancelled', message: 'Cancelled. Nothing was sent.' })
  })

  it('finds which of the account’s passkeys this device holds', async () => {
    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(OTHER))
    const found = findDevicePasskey([CREDENTIAL, OTHER])
    expect(browser.get).toHaveBeenCalledTimes(1)
    expect(await found).toBe(OTHER)
    const { publicKey } = browser.get.mock.calls[0][0]!
    expect(publicKey!.allowCredentials!.map(c => bytesOf(c.id))).toEqual(['credential-one', 'credential-two'])
  })

  it('keeps working when the browser blocks storage', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('Blocked', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Blocked', 'SecurityError')
    })
    expect(() => rememberPasskey(CREDENTIAL)).not.toThrow()
    expect(isOnThisDevice(CREDENTIAL)).toBe(false)

    const browser = installPasskeys()
    browser.get.mockResolvedValue(signedCredential(CREDENTIAL))
    await expect(signWithPasskey(start())).resolves.toMatchObject({ id: CREDENTIAL })
  })
})

describe('defaultDeviceName', () => {
  it('names the device from its user agent', () => {
    const names = {
      'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148': 'iPhone',
      'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15': 'iPad',
      'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130.0 Mobile Safari/537.36': 'Android phone',
      'Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 Chrome/130.0 Safari/537.36': 'Android tablet',
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15': 'Mac',
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36': 'Windows PC',
      'Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 Chrome/130.0 Safari/537.36': 'Chromebook',
      'Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0': 'Linux computer',
      'Mozilla/5.0 (darwin) AppleWebKit/537.36 (KHTML, like Gecko) jsdom/29.1.1': 'My device',
    }
    for (const [userAgent, name] of Object.entries(names)) expect(defaultDeviceName(userAgent, 0), userAgent).toBe(name)
    // iPadOS asks for desktop sites as a Mac; only an iPad has a touch screen.
    expect(defaultDeviceName('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15', 5)).toBe('iPad')
  })
})

describe('deviceNameProblem', () => {
  it('wants 1 to 40 characters', () => {
    expect(deviceNameProblem('iPhone')).toBeNull()
    expect(deviceNameProblem('   ')).toMatch(/Give this device a name/)
    expect(deviceNameProblem('x'.repeat(41))).toBe('Use at most 40 characters.')
  })
})
