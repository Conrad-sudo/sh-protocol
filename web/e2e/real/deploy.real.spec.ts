import { expect, test } from '@playwright/test'
import { parseAbi, parseEther } from 'viem'
import { sepolia } from 'viem/chains'
import { formatTokenAmount } from '../../src/lib/format.ts'
import { installRealOwner, publicClient, requireLocalFork, signUpAndDeploy } from './realSetup.ts'

/*
 * The whole journey for real: sign up against the running API, prove a fresh key owns the account,
 * have that key sign and send deployWallet on the local Sepolia fork, then top the new wallet up
 * from the dashboard. Nothing is mocked. Run with: E2E_REAL=1 npx playwright test real
 */

test('a new user creates and funds a wallet on the local fork', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-light', 'One real deploy per run is enough.')
  test.setTimeout(180_000)
  await requireLocalFork(request)

  const owner = await installRealOwner(page)
  const account = await signUpAndDeploy(page, request, 'e2e-deploy')
  expect(owner.sentOn).toEqual([sepolia.id])
  await page.screenshot({ path: testInfo.outputPath('deployed.png') })

  // What the API recorded, read back as the same user.
  const wallet = await account.readWallet()
  expect(wallet).toMatchObject({
    owner: owner.account.address,
    is_owner: true,
    paused: false,
    spending: { daily_limit_usd: 100, window_hours: 24 },
  })

  // And what the chain says, without the API in between.
  const code = await publicClient.getCode({ address: wallet.address })
  expect(code?.length ?? 0).toBeGreaterThan(2)
  const onChainOwner = await publicClient.readContract({
    address: wallet.address,
    abi: parseAbi(['function owner() view returns (address)']),
    functionName: 'owner',
  })
  expect(onChainOwner).toBe(owner.account.address)
  expect(await publicClient.getBalance({ address: wallet.address })).toBeGreaterThanOrEqual(parseEther('1'))

  // Topping up from the dashboard with the connected wallet.
  const before = await publicClient.getBalance({ address: wallet.address })
  await page.getByRole('button', { name: 'Add funds' }).click()
  const drawer = page.getByRole('dialog').filter({ hasText: 'Add funds' })
  await drawer.getByLabel('Amount').fill('0.5')
  await drawer.getByRole('button', { name: 'Send' }).click()
  await expect(drawer.getByText('Received. Your balance is up to date.')).toBeVisible({ timeout: 60_000 })
  const after = await publicClient.getBalance({ address: wallet.address })
  expect(after - before).toBe(parseEther('0.5'))
  expect(owner.sentOn).toEqual([sepolia.id, sepolia.id])

  // The dashboard shows the new balance, read back through the API.
  await page.keyboard.press('Escape')
  await expect(drawer).toBeHidden()
  const ethRow = page.getByRole('row').filter({ has: page.getByRole('rowheader', { name: 'ETH', exact: true }) })
  await expect(ethRow).toContainText(formatTokenAmount(after.toString(), 18))
  await page.screenshot({ path: testInfo.outputPath('funded.png') })
})
