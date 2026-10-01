interface ImportMetaEnv {
  /** API origin when it is not served from the same origin as the site (e.g. https://api.mitfah.com). */
  readonly VITE_API_URL?: string
  /** Reown (WalletConnect) project ID. Empty hides WalletConnect. */
  readonly VITE_WALLETCONNECT_PROJECT_ID?: string
}
