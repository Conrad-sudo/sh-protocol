interface ImportMetaEnv {
  /** API origin when it is not served from the same origin as the site (e.g. https://api.mitfah.com). */
  readonly VITE_API_URL?: string
  /** Google OAuth client ID. Empty disables Google sign-in. Set in vite.config.ts. */
  readonly VITE_GOOGLE_CLIENT_ID: string
}
