import type { Page } from '@playwright/test'

/** A transaction as the app asks the wallet to send it (EIP-1193 `eth_sendTransaction`). */
export interface WalletTx {
  from: string
  to: string
  data?: string
  value?: string
  gas?: string
}

/** The Node side of the fake wallet: what happens when the page asks it to sign or send. */
export interface WalletBackend {
  address: string
  chainId: number
  /** `personal_sign`. `message` is hex-encoded, as the app sends it. */
  signMessage: (message: string) => Promise<string>
  /**
   * `eth_sendTransaction`. The request carries no chain id, so `chainId` is the network the wallet
   * was on when asked, as a real wallet would use.
   */
  sendTransaction: (tx: WalletTx, chainId: number) => Promise<string>
  /**
   * Any other request the app sends through the wallet — reads such as eth_getTransactionReceipt,
   * which a real wallet forwards to its node. Without it those requests fail.
   */
  request?: (method: string, params: unknown[]) => Promise<unknown>
}

export const FAKE_WALLET_NAME = 'Test Wallet'

/**
 * Installs a browser wallet the app discovers like any other (EIP-6963). Account and network
 * requests are answered in the page; everything else — signing, sending and node reads — is handed
 * to `backend` in Node, so a spec can return fixed values or use a real key against a local fork.
 *
 * Call before the first `page.goto`. The wallet starts disconnected on every page load, like a
 * wallet that has not yet approved this site.
 */
export async function installFakeWallet(page: Page, backend: WalletBackend) {
  await page.exposeFunction('__fakeWalletBackend', (method: string, params: unknown[], chainId: number) => {
    if (method === 'personal_sign') return backend.signMessage(params[0] as string)
    if (method === 'eth_sendTransaction') return backend.sendTransaction(params[0] as WalletTx, chainId)
    if (backend.request) return backend.request(method, params)
    throw new Error(`The test wallet does not support ${method}`)
  })

  await page.addInitScript(
    ({ address, chainId: initialChainId, name }) => {
      type Listener = (...args: unknown[]) => void
      const listeners = new Map<string, Set<Listener>>()
      const emit = (event: string, ...args: unknown[]) => listeners.get(event)?.forEach(fn => fn(...args))
      const hex = (n: number) => `0x${n.toString(16)}`
      let chainId = initialChainId
      let connected = false
      // Looked up per call: the exposed function is bound by Playwright, not by this script.
      const backendCall = (method: string, params: unknown[]) =>
        (window as unknown as { __fakeWalletBackend: (m: string, p: unknown[], c: number) => Promise<unknown> })
          .__fakeWalletBackend(method, params, chainId)

      const provider = {
        async request({ method, params = [] }: { method: string; params?: unknown[] }) {
          switch (method) {
            case 'eth_chainId':
              return hex(chainId)
            case 'net_version':
              return String(chainId)
            case 'eth_accounts':
              return connected ? [address] : []
            case 'eth_requestAccounts':
              connected = true
              emit('connect', { chainId: hex(chainId) })
              return [address]
            case 'wallet_requestPermissions':
              connected = true
              return [{ parentCapability: 'eth_accounts', caveats: [{ type: 'restrictReturnedAccounts', value: [address] }] }]
            case 'wallet_revokePermissions':
              connected = false
              return null
            case 'wallet_switchEthereumChain': {
              chainId = Number((params[0] as { chainId: string }).chainId)
              emit('chainChanged', hex(chainId))
              return null
            }
            default:
              return backendCall(method, params)
          }
        },
        on(event: string, fn: Listener) {
          if (!listeners.has(event)) listeners.set(event, new Set())
          listeners.get(event)!.add(fn)
        },
        removeListener(event: string, fn: Listener) {
          listeners.get(event)?.delete(fn)
        },
      }

      const icon =
        'data:image/svg+xml,' +
        encodeURIComponent(
          '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect width="24" height="24" rx="6" fill="#0f766e"/></svg>',
        )
      const detail = Object.freeze({
        info: { uuid: '6f1c7d0e-3b7a-4f0e-9a51-2d1f7f3b9c11', name, icon, rdns: 'com.mitfah.testwallet' },
        provider,
      })
      const announce = () => window.dispatchEvent(new CustomEvent('eip6963:announceProvider', { detail }))
      window.addEventListener('eip6963:requestProvider', announce)
      announce()
    },
    { address: backend.address, chainId: backend.chainId, name: FAKE_WALLET_NAME },
  )
}
