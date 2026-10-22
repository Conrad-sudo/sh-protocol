/** A link token: the server mints them with token_urlsafe(24). Anything else can't be one. */
const TOKEN = /^[A-Za-z0-9_-]{16,128}$/

/** The token in a /c/<token> address, or null when there is none worth asking the server about. */
export function tokenFromPath(pathname: string): string | null {
  const match = /^\/c\/([^/]+)\/?$/.exec(pathname)
  return match && TOKEN.test(match[1]) ? match[1] : null
}
