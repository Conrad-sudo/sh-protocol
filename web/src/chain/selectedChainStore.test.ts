import { afterEach, describe, expect, it, vi } from 'vitest'
import { readStoredChain, resolveChain, writeStoredChain } from './selectedChainStore'

const SEPOLIA = 11155111
const MAINNET = 1
const BSC = 56

describe('resolveChain', () => {
  it('keeps the stored choice while the user has a wallet there', () => {
    expect(resolveChain(BSC, [SEPOLIA, BSC], [MAINNET, BSC, SEPOLIA])).toBe(BSC)
  })

  it('falls back to the first wallet when the stored choice has none', () => {
    expect(resolveChain(MAINNET, [SEPOLIA, BSC], [MAINNET, BSC, SEPOLIA])).toBe(SEPOLIA)
    expect(resolveChain(null, [BSC], [MAINNET, BSC])).toBe(BSC)
  })

  it('uses the served networks when the user has no wallet yet', () => {
    expect(resolveChain(BSC, [], [MAINNET, BSC])).toBe(BSC)
    expect(resolveChain(SEPOLIA, [], [MAINNET, BSC])).toBe(MAINNET)
  })

  it('is null when there is nothing to show', () => {
    expect(resolveChain(null, [], [])).toBeNull()
  })
})

describe('stored chain', () => {
  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('is remembered per account', () => {
    writeStoredChain(7, SEPOLIA)
    writeStoredChain(8, BSC)
    expect(readStoredChain(7)).toBe(SEPOLIA)
    expect(readStoredChain(8)).toBe(BSC)
    expect(readStoredChain(9)).toBeNull()
  })

  it('is ignored while signed out', () => {
    writeStoredChain(null, SEPOLIA)
    expect(localStorage.length).toBe(0)
    expect(readStoredChain(null)).toBeNull()
  })

  it('ignores a value that is not a chain id', () => {
    localStorage.setItem('mitfah-chain:7', 'mainnet')
    expect(readStoredChain(7)).toBeNull()
    localStorage.setItem('mitfah-chain:7', '-1')
    expect(readStoredChain(7)).toBeNull()
  })

  it('carries on when storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    expect(() => writeStoredChain(7, SEPOLIA)).not.toThrow()
    expect(readStoredChain(7)).toBeNull()
  })
})
