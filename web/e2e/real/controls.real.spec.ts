import { expect, test } from '@playwright/test'
import { parseAbi, parseEther, type Address, type Hex } from 'viem'
import { sepolia } from 'viem/chains'
import { addPasskeyAuthenticator } from '../helpers.ts'
import { installRealOwner, publicClient, requireLocalFork, signInAndDeploy } from './realSetup.ts'

/*
 * The owner's controls for real, on the local Sepolia fork: a fresh owner creates a wallet, then
 * pauses it, withdraws while paused, unpauses, raises the limit, revokes its device's passkey and adds
 * it back, stops counting a token — each signed by that owner in the page and checked on chain afterwards.
 * Run with: E2E_REAL=1 npx playwright test real
 */

const abi = parseAbi([
  'function paused() view returns (bool)',
  'function getRemainingBudget() view returns (int256)',
  'function getSessions() view returns (bytes32[] keyIds, uint48[] validUntils)',
  'function isSessionActive(bytes32) view returns (bool)',
  'function isWatched(address) view returns (bool)',
])

/** The name the device's passkey is made under during onboarding. */
const DEVICE = 'Test laptop'

test('the owner changes the wallet from Controls on the local fork', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-light', 'One real run per suite is enough.')
  test.setTimeout(300_000)
  await requireLocalFork(request)

  const authenticator = await addPasskeyAuthenticator(page)
  const owner = await installRealOwner(page)
  const account = await signInAndDeploy(page, request, owner, { passkey: { deviceName: DEVICE } })
  const wallet = await account.readWallet()
  const address = wallet.address
  const onChain = {
    paused: () => publicClient.readContract({ address, abi, functionName: 'paused' }),
    remaining: () => publicClient.readContract({ address, abi, functionName: 'getRemainingBudget' }),
    sessions: () => publicClient.readContract({ address, abi, functionName: 'getSessions' }),
    active: (keyId: Hex) => publicClient.readContract({ address, abi, functionName: 'isSessionActive', args: [keyId] }),
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

  // The device's passkey, made during onboarding, is the one the wallet trusts.
  const [device] = wallet.session.keys
  expect(device).toMatchObject({ device_name: DEVICE, active: true })
  expect((await onChain.sessions())[0]).toEqual([device.key_id])
  expect(await onChain.active(device.key_id)).toBe(true)
  const row = page.locator('.mf-control-row').filter({ has: page.getByRole('heading', { name: DEVICE, exact: true }) })
  await expect(row.getByText('This device')).toBeVisible()

  // Revoke it: one click. The wallet forgets the key; the account keeps the passkey.
  await page.getByRole('button', { name: `Revoke ${DEVICE}` }).click()
  await confirmed(`${DEVICE} can no longer approve payments on Sepolia.`)
  expect((await onChain.sessions())[0]).toEqual([])
  expect(await onChain.active(device.key_id)).toBe(false)
  expect((await account.readWallet()).session.keys).toEqual([])

  // Add it back: asks first, and grants the same passkey again, with no new prompt to make one.
  await page.getByRole('button', { name: 'Add this device', exact: true }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Add device' }).click()
  await confirmed(new RegExp(`^${DEVICE} can approve payments on Sepolia until `))
  expect((await onChain.sessions())[0]).toEqual([device.key_id])
  expect(await onChain.active(device.key_id)).toBe(true)
  expect(await authenticator.credentials()).toHaveLength(1)
  await expect(page.getByRole('button', { name: `Renew ${DEVICE}` })).toBeVisible()

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
    session: { keys: [{ key_id: device.key_id, device_name: DEVICE, active: true }] },
  })
  expect(owner.sentOn).toEqual(Array(8).fill(sepolia.id))
  await page.screenshot({ path: testInfo.outputPath('controls-after.png'), fullPage: true })
})
