import { anvil } from 'viem/chains'
import { describe, expect, it } from 'vitest'
import caddyfile from '../../deploy/Caddyfile?raw'
import indexHtml from '../index.html?raw'
import { SUPPORTED_CHAINS } from './wallet/chains'

/**
 * The Content-Security-Policy deploy/Caddyfile sends is written by hand, so it can fall out of step
 * with the app: an inline script edited without its hash, or a chain whose RPC the policy doesn't
 * name. Either would break the live site quietly, in the browser console only.
 */

/** The policy, as directive -> its sources. */
function policy(): Map<string, string[]> {
  const value = caddyfile.match(/Content-Security-Policy "([^"]+)"/)?.[1]
  if (!value) throw new Error('deploy/Caddyfile sends no Content-Security-Policy')
  return new Map(
    value
      .split(';')
      .map(directive => directive.trim().split(/\s+/))
      .filter(([name]) => name)
      .map(([name, ...sources]) => [name, sources]),
  )
}

/** Whether a scheme-and-host source list (https://host, https://*.host) allows `url`. */
function allows(sources: string[], url: string): boolean {
  const { protocol, host } = new URL(url)
  return sources.some(source => {
    const match = source.match(/^([a-z]+:)\/\/(\*\.)?([^/]+)$/)
    if (!match || match[1] !== protocol) return false
    return match[2] ? host.endsWith(`.${match[3]}`) : host === match[3]
  })
}

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
}

describe('the security headers in deploy/Caddyfile', () => {
  it('allow exactly the inline scripts in index.html, by hash', async () => {
    const inline = [...indexHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(([, body]) => body)
    expect(inline.length).toBeGreaterThan(0)
    const hashes = await Promise.all(inline.map(async body => `'sha256-${await sha256(body)}'`))
    const scriptSrc = policy().get('script-src') ?? []
    for (const hash of hashes) expect(scriptSrc).toContain(hash)
    // No hash for a script that is gone: it would let that old text run if it came back.
    expect(scriptSrc.filter(source => source.startsWith("'sha256-"))).toHaveLength(hashes.length)
  })

  it('let the app reach the RPC of every live chain it reads from', () => {
    const connectSrc = policy().get('connect-src') ?? []
    for (const chain of SUPPORTED_CHAINS) {
      if (chain.id === anvil.id) continue // a local node, never served from the live site
      const url = chain.rpcUrls.default.http[0]
      expect(allows(connectSrc, url), `${chain.name}: ${url}`).toBe(true)
    }
  })

  it('never allow eval, other inline scripts, plugins or framing', () => {
    const csp = policy()
    expect(csp.get('script-src')).not.toContain("'unsafe-eval'")
    expect(csp.get('script-src')).not.toContain("'unsafe-inline'")
    expect(csp.get('object-src')).toEqual(["'none'"])
    expect(csp.get('frame-ancestors')).toEqual(["'none'"])
  })
})
