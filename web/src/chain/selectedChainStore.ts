const key = (userId: number) => `mitfah-chain:${userId}`

export function readStoredChain(userId: number | null): number | null {
  if (userId === null) return null
  try {
    const value = Number(localStorage.getItem(key(userId)))
    return Number.isInteger(value) && value > 0 ? value : null
  } catch {
    return null
  }
}

export function writeStoredChain(userId: number | null, chainId: number) {
  if (userId === null) return
  try {
    localStorage.setItem(key(userId), String(chainId))
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
}

/**
 * The network to show. A stored choice wins if the user still has a wallet there; otherwise their
 * first wallet's network; with no wallets, the first network the server offers.
 */
export function resolveChain(preferred: number | null, walletChains: number[], served: number[]): number | null {
  const candidates = walletChains.length > 0 ? walletChains : served
  if (preferred !== null && candidates.includes(preferred)) return preferred
  return candidates[0] ?? null
}
