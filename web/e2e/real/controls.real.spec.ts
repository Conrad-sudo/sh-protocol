import { expect, test } from '@playwright/test'
import { parseAbi, parseEther, zeroAddress, type Address } from 'viem'
import { sepolia } from 'viem/chains'
import { installRealOwner, publicClient, requireLocalFork, signUpAndDeploy } from './realSetup.ts'

/*
 * The owner's controls for real, on the local Sepolia fork: a fresh owner creates a wallet, then
 * pauses it, withdraws while paused, unpauses, raises the limit, turns the assistant off and on again, stops
 * counting a token — each signed by that owner in the page and checked on chain afterwards.
 * Run with: E2E_REAL=1 npx playwright test real
 */

const abi = parseAbi([
  'function paused() view returns (bool)',
  'function getRemainingBudget() view returns (int256)',
  'function currentSession() view returns (address)',
  'function isSessionActive(address) view returns (bool)',
  'function isWatched(address) view returns (bool)',
])

test('the owner changes the wallet from Controls on the local fork', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-light', 'One real run per suite is enough.')
  test.setTimeout(300_000)
  await requireLocalFork(request)

  const owner = await installRealOwner(page)
  const account = await signUpAndDeploy(page, request, 'e2e-controls')
  const wallet = await account.readWallet()
  const address = wallet.address
  const onChain = {
    paused: () => publicClient.readContract({ address, abi, functionName: 'paused' }),
    remaining: () => publicClient.readContract({ address, abi, functionName: 'getRemainingBudget' }),
    sessionKey: () => publicClient.readContract({ address, abi, functionName: 'currentSession' }),
    active: (key: Address) => publicClient.readContract({ address, abi, functionName: 'isSessionActive', args: [key] }),
    watched: (token: Address) => publicClient.readContract({ address, abi, functionName: 'isWatched', args: [token] }),
  }
  const confirmed = (text: string | RegExp) => expect(page.getByText(text)).toBeVisible({ timeout: 60_000 })

  // Through the app's own link, so the browser wallet stays connected.
  await page.getByRole('link', { name: 'Controls' }).click()
  await expect(page.getByText('Owner connected')).toBeVisible()

  // Pause: one click.
  await page.getByRole('button', { name: 'Pause wallet' }).click()
  await confirmed('Wallet paused. Nothing can go out until you unpause it.')
  expect(await onChain.paused()).toBe(true)
  await expect(page.getByRole('button', { name: 'Unpause' })).toBeVisible()

  // A withdrawal still goes through while paused.
  const before = await publicClient.getBalance({ address })
  await page.getByRole('button', { name: 'Withdraw' }).click()
  const drawer = page.getByRole('dialog').filter({ hasText: 'Move funds out' })
  await drawer.getByLabel('Amount').fill('0.25')
  await drawer.getByRole('button', { name: 'Withdraw' }).click()
  await expect(drawer.getByText('Sent. The balance above is up to date.')).toBeVisible({ timeout: 60_000 })
  expect(before - (await publicClient.getBalance({ address }))).toBe(parseEther('0.25'))
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()

  // Unpause: asks first.
  await page.getByRole('button', { name: 'Unpause' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Unpause wallet' }).click()
  await confirmed('Wallet unpaused.')
  expect(await onChain.paused()).toBe(false)

  // Raise the limit from $100 to $250: asks first. Nothing has been spent, so all of it is left.
  expect(await onChain.remaining()).toBe(parseEther('100'))
  const limit = page.getByLabel('Limit per period')
  await limit.fill('250')
  await page.locator('form').filter({ has: limit }).getByRole('button', { name: 'Save' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Raise limit' }).click()
  await confirmed('Spending limit set to $250.00.')
  expect(await onChain.remaining()).toBe(parseEther('250'))

  // Turn the assistant off: one click. The wallet drops the key, and Mitfah forgets it.
  const key = wallet.session.key!
  expect(await onChain.sessionKey()).toBe(key)
  expect(await onChain.active(key)).toBe(true)
  await page.getByRole('button', { name: 'Turn off assistant' }).click()
  await confirmed('Assistant turned off. It can no longer act for this wallet.')
  expect(await onChain.sessionKey()).toBe(zeroAddress)
  expect((await account.readWallet()).session.key).toBeNull()

  // Turn it back on: asks first, and grants a brand-new key, never the one just revoked.
  await page.getByRole('button', { name: 'Turn on assistant' }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Turn on assistant' }).click()
  await confirmed(/^Assistant turned on until /)
  const newKey = await onChain.sessionKey()
  expect(newKey).not.toBe(zeroAddress)
  expect(newKey).not.toBe(key)
  expect(await onChain.active(newKey)).toBe(true)
  await expect(page.getByRole('button', { name: 'Renew' })).toBeVisible()

  // Stop counting a token: asks first.
  const token = wallet.spending.watched_tokens[0]
  const name = token.ticker!.toUpperCase()
  expect(await onChain.watched(token.address)).toBe(true)
  await page.getByRole('button', { name: `Stop counting ${name}` }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: `Stop counting ${name}` }).click()
  await confirmed(`${name} no longer counts toward your limit.`)
  expect(await onChain.watched(token.address)).toBe(false)

  // The API agrees, and every transaction went to Sepolia: the deploy plus seven changes.
  expect(await account.readWallet()).toMatchObject({
    paused: false,
    spending: { daily_limit_usd: 250 },
    session: { key: newKey, wallet_key: newKey, is_app_key: true, active: true },
  })
  expect(owner.sentOn).toEqual(Array(8).fill(sepolia.id))
  await page.screenshot({ path: testInfo.outputPath('controls-after.png'), fullPage: true })
})
