import { onTestFinished, vi, type Mock } from 'vitest'
import { fromBase64Url } from '../lib/passkey'

/*
 * jsdom has no WebAuthn. These stand in for the browser's side of a passkey: navigator.credentials
 * with create/get mocks, and credentials shaped as the browser returns them. Nothing is signed;
 * the server's checks are py_webauthn's, tested in app/tests.
 */

/** The browser's class, so the app sees passkey support. */
class FakePublicKeyCredential {}

export interface FakeAuthenticator {
  create: Mock<(options?: CredentialCreationOptions) => Promise<Credential | null>>
  get: Mock<(options?: CredentialRequestOptions) => Promise<Credential | null>>
}

const text = (value: string) => new TextEncoder().encode(value).buffer as ArrayBuffer

/**
 * Installs WebAuthn for one test (removed when it finishes). By default every prompt stays open,
 * like a user who hasn't answered yet; a test says what each one does.
 */
export function installPasskeys(): FakeAuthenticator {
  const create: FakeAuthenticator['create'] = vi.fn(() => new Promise<Credential | null>(() => {}))
  const get: FakeAuthenticator['get'] = vi.fn(() => new Promise<Credential | null>(() => {}))
  vi.stubGlobal('PublicKeyCredential', FakePublicKeyCredential)
  Object.defineProperty(navigator, 'credentials', { configurable: true, value: { create, get } })
  onTestFinished(() => {
    delete (navigator as { credentials?: unknown }).credentials
  })
  return { create, get }
}

/** A new passkey as create() resolves it, for `credentialId` (base64url). */
export function madeCredential(credentialId: string, { attachment = 'platform' }: { attachment?: string } = {}) {
  return Object.assign(new FakePublicKeyCredential(), {
    id: credentialId,
    rawId: fromBase64Url(credentialId),
    type: 'public-key',
    authenticatorAttachment: attachment,
    response: {
      clientDataJSON: text('{"type":"webauthn.create"}'),
      attestationObject: text('attestation'),
      getTransports: () => ['internal', 'hybrid'],
      getPublicKey: () => text('public key'),
      getPublicKeyAlgorithm: () => -7,
      getAuthenticatorData: () => text('authenticator data'),
    },
    getClientExtensionResults: () => ({}),
  }) as unknown as Credential
}

/** A passkey's answer as get() resolves it, for `credentialId` (base64url). */
export function signedCredential(credentialId: string, { attachment = 'platform' }: { attachment?: string } = {}) {
  return Object.assign(new FakePublicKeyCredential(), {
    id: credentialId,
    rawId: fromBase64Url(credentialId),
    type: 'public-key',
    authenticatorAttachment: attachment,
    response: {
      clientDataJSON: text('{"type":"webauthn.get"}'),
      authenticatorData: text('authenticator data'),
      signature: text('signature'),
      userHandle: text('user handle'),
    },
    getClientExtensionResults: () => ({}),
  }) as unknown as Credential
}

/** How a browser turns a prompt down: closed, timed out, or (Safari) asked for without a tap. */
export function refused(name = 'NotAllowedError') {
  return new DOMException('The operation either timed out or was not allowed.', name)
}

/** base64url of `value`, as the server writes binary fields. */
export function b64url(value: string) {
  return btoa(value).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
