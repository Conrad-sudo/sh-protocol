import { describe, expect, it } from 'vitest'
import { chainLogoUrl, chainName, explorerName, explorerUrl, isSupportedChainId, SUPPORTED_CHAINS } from './chains'

describe('chains', () => {
  it('serves every chain the API can, Base among them', () => {
    expect(SUPPORTED_CHAINS.map(chain => chain.id)).toEqual([11155111, 1, 56, 42161, 8453, 31337])
    expect(isSupportedChainId(8453)).toBe(true)
  })

  it('names Base, shows its logo, and links to its explorer', () => {
    expect(chainName(8453)).toBe('Base')
    expect(chainLogoUrl(8453)).toBe('/chainlogos/8453.svg')
    expect(explorerName(8453)).toBe('Basescan')
    expect(explorerUrl(8453, 'tx', '0xabc')).toBe('https://basescan.org/tx/0xabc')
    expect(explorerUrl(8453, 'address', '0xdef')).toBe('https://basescan.org/address/0xdef')
  })

  it('gives every real network a logo named by chain ID, and a bare local node none', () => {
    for (const chain of SUPPORTED_CHAINS) {
      if (chain.id === 31337) expect(chainLogoUrl(chain.id)).toBeNull()
      else expect(chainLogoUrl(chain.id)).toMatch(/^\/chainlogos\/\d+\.svg$/)
    }
  })
})
