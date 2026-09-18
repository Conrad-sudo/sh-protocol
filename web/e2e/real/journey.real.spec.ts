import { expect, test } from '@playwright/test'
import { FAKE_WALLET_NAME } from '../fakeWallet.ts'
import { parseAbi, parseEther } from 'viem'
import { sepolia } from 'viem/chains'
import { installRealOwner, publicClient, requireLocalFork, signUpAndDeploy } from './realSetup.ts'

/*
 * One person, one sitting, nothing mocked: sign up, create a wallet on the local Sepolia fork, top
 * it up, save a contact, pause and unpause the wallet, withdraw, and reload the page in the middle
 * to prove the session and the wallet come back.
 *
 * The other real specs each prove one thing deeply (deploy.real the deploy, controls.real every
 * owner control). This one walks the whole path in order, through the app's own links, which is
 * where the seams are: a page loaded on demand after a reload, and a wallet state carried across.
 * Run with: E2E_REAL=1 npx playwright test real/journey --project=desktop-light
 */

const abi = parseAbi(['function paused() view returns (bool)'])
const NEIGHBOUR = '0x90F79bf6EB2c4f870365E785982E1f101E93b906'

test('one sitting: sign up, create, fund, save a contact, pause, withdraw, reload', async ({
  page,
  request,
}, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-light', 'One real run per suite is enough.')
  test.setTimeout(300_000)
  await requireLocalFork(request)

  const owner = await installRealOwner(page)
  const account = await signUpAndDeploy(page, request, 'e2e-journey')
  const { address } = await account.readWallet()
  const paused = () => publicClient.readContract({ address, abi, functionName: 'paused' })
  const confirmed = (text: string) => expect(page.getByText(text)).toBeVisible({ timeout: 60_000 })

  // 1. Top up from the dashboard.
  const funded = await publicClient.getBalance({ address })
  await page.getByRole('button', { name: 'Add funds' }).click()
  const fund = page.getByRole('dialog').filter({ hasText: 'Add funds' })
  await fund.getByLabel('Amount').fill('0.5')
  await fund.getByRole('button', { name: 'Send' }).click()
  await expect(fund.getByText('Received. Your balance is up to date.')).toBeVisible({ timeout: 60_000 })
  await page.keyboard.press('Escape')
  expect(await publicClient.getBalance({ address })).toBe(funded + parseEther('0.5'))

  // 2. Save someone to pay, from the page's own navigation. (The sidebar's logo links to the
  // dashboard too, so the nav landmark is what's clicked.)
  const nav = page.getByRole('navigation', { name: 'Main' })
  await nav.getByRole('link', { name: 'Contacts' }).click()
  await expect(page.getByRole('heading', { name: 'No contacts yet' })).toBeVisible()
  await page.getByRole('button', { name: 'Add contact' }).click()
  const add = page.getByRole('dialog')
  await add.getByLabel('Name').fill('neighbour')
  await add.getByLabel('Address').fill(NEIGHBOUR)
  await add.getByRole('button', { name: 'Continue' }).click()
  await add.getByRole('button', { name: 'Save contact' }).click()
  await expect(page.getByRole('heading', { name: 'Your contacts (1)' })).toBeVisible()

  // 3. Reload in the middle: the session, the wallet and the contact all come back.
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Your contacts (1)' })).toBeVisible()
  await expect(page.getByText('neighbour')).toBeVisible()
  await nav.getByRole('link', { name: 'Dashboard', exact: true }).click()
  await expect(page.getByText('Your Mitfah wallet on Sepolia')).toBeVisible()

  // 4. The emergency brake, signed by the owner's own wallet. A browser wallet forgets the site on
  // a reload, so it is connected again first — from inside the dialog, where the app asks for it.
  await page.getByRole('button', { name: 'Pause wallet' }).click()
  const pauseDialog = page.getByRole('dialog').filter({ hasText: 'Pause this wallet?' })
  await pauseDialog.getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await expect(pauseDialog.getByText('Owner connected')).toBeVisible()
  // The dialog closes once the network confirms, so the dashboard itself says whether it worked.
  await pauseDialog.getByRole('button', { name: 'Pause wallet' }).click()
  // "Unpause", not "Unpause wallet": the dialog's own button is still on its way out.
  await expect(page.getByRole('button', { name: 'Unpause', exact: true })).toBeVisible({ timeout: 120_000 })
  await confirmed('This wallet is paused')
  expect(await paused()).toBe(true)

  // 5. Withdrawing still works while paused — that is the point of the brake.
  const before = await publicClient.getBalance({ address })
  await page.getByRole('button', { name: 'Withdraw' }).click()
  const withdraw = page.getByRole('dialog').filter({ hasText: 'Move funds out' })
  await withdraw.getByLabel('Amount').fill('0.25')
  await withdraw.getByRole('button', { name: 'Withdraw' }).click()
  await expect(withdraw.getByText('Sent. The balance above is up to date.')).toBeVisible({ timeout: 60_000 })
  expect(before - (await publicClient.getBalance({ address }))).toBe(parseEther('0.25'))
  await page.keyboard.press('Escape')

  // 6. Back to normal.
  await page.getByRole('button', { name: 'Unpause', exact: true }).click()
  const unpauseDialog = page.getByRole('dialog').filter({ hasText: 'Unpause this wallet?' })
  await unpauseDialog.getByRole('button', { name: 'Unpause wallet' }).click()
  await expect(page.getByText('Active', { exact: true })).toBeVisible({ timeout: 120_000 })
  expect(await paused()).toBe(false)

  // Deploy, top-up, pause, withdraw, unpause: five transactions, all on the fork.
  expect(owner.sentOn).toEqual(Array(5).fill(sepolia.id))
  await page.screenshot({ path: testInfo.outputPath('journey-end.png') })
})
