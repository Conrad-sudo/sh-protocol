/** Google's OAuth client ID. Must match the API's GOOGLE_CLIENT_ID, which checks the token's audience. */
export const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID ?? ''

/** Google sign-in is offered only when a client ID is configured. */
export const googleEnabled = GOOGLE_CLIENT_ID !== ''
