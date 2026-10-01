import { expect, test, type Page } from '@playwright/test'
import { getAddress } from 'viem'
import { FAKE_WALLET_NAME, installFakeWallet } from './fakeWallet.ts'
import { expectNoSidewaysScroll, expectTheme, isApiUrl, ME, OWNER, snap, TOKEN } from './helpers.ts'

/*
 * The Contacts page — the people the assistant may pay — at real screen sizes, in both themes,
 * against a mocked API that behaves like app/api.py. Saving one takes the signed-in wallet's
 * EIP-712 signature, which the fake wallet gives.
 */

const SAM = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const ALEX = '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC'
const NEW = '0x90F79bf6EB2c4f870365E785982E1f101E93b906'
// The longest name the server accepts, with nowhere to break a line.
const LONG_NAME = 'the-landlord-of-the-flat-on-maple-street-who-is-paid-every-month'
const SEPOLIA = 11155111
const SIGNATURE = `0x${'cd'.repeat(65)}`

interface Contact {
  name: string
  address: string
}

async function mockServer(page: Page, contacts: Contact[]) {
  let saved = [...contacts]
  let nonces = 0
  const posted: (Contact & { nonce: string; signature: string })[] = []
  const deleted: string[] = []
  /** The typed data the wallet was asked to sign, as eth_signTypedData_v4 received it. */
  const signed: { primaryType: string; message: Record<string, string> }[] = []

  await installFakeWallet(page, {
    address: OWNER,
    chainId: SEPOLIA,
    signMessage: async () => {
      throw new Error('the contacts page signs no plain message')
    },
    signTypedData: async typedData => {
      signed.push(JSON.parse(typedData))
      return SIGNATURE
    },
    sendTransaction: async () => {
      throw new Error('the contacts page sends no transaction')
    },
  })

  await page.route(isApiUrl, route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
    if (path === '/api/contacts' && method === 'GET') {
      return route.fulfill({ json: { contacts: [...saved].sort((a, b) => (a.name < b.name ? -1 : 1)) } })
    }
    if (path === '/api/contacts/prepare' && method === 'POST') {
      const body = request.postDataJSON() as Contact
      nonces += 1
      return route.fulfill({
        json: {
          domain: { name: 'Mitfah', version: '1' },
          types: {
            AddContact: [
              { name: 'name', type: 'string' },
              { name: 'address', type: 'address' },
              { name: 'nonce', type: 'string' },
            ],
          },
          primaryType: 'AddContact',
          message: { name: body.name.trim().toLowerCase(), address: getAddress(body.address), nonce: `nonce${nonces}` },
        },
      })
    }
    if (path === '/api/contacts' && method === 'POST') {
      const body = request.postDataJSON() as Contact & { nonce: string; signature: string }
      posted.push(body)
      const contact = { name: body.name, address: body.address }
      saved = [...saved.filter(c => c.name !== body.name), contact]
      return route.fulfill({ status: 201, json: contact })
    }
    if (path.startsWith('/api/contacts/') && method === 'DELETE') {
      deleted.push(path)
      const name = decodeURIComponent(path.slice('/api/contacts/'.length))
      saved = saved.filter(c => c.name !== name)
      return route.fulfill({ json: { status: 'deleted', name } })
    }
    switch (path) {
      case '/api/auth/refresh':
        return route.fulfill({ json: TOKEN })
      case '/api/me':
        return route.fulfill({ json: ME })
      case '/api/chains':
        return route.fulfill({ json: { chains: [] } })
      default:
        return route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    }
  })
  return { posted, deleted, signed }
}

/** Connects the fake wallet from the contact dialog: it is the wallet that approves the contact. */
async function connectWallet(page: Page) {
  await page.getByRole('dialog').getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()
  await expect(page.getByRole('dialog')).toHaveCount(1)
}

test('the contacts list, and adding someone after checking the address', async ({ page }, testInfo) => {
  expect(LONG_NAME).toHaveLength(64)
  const { posted, signed } = await mockServer(page, [
    { name: 'alex', address: ALEX },
    { name: LONG_NAME, address: NEW },
  ])
  await page.goto('/contacts')

  await expect(page.getByRole('heading', { name: 'Contacts', level: 1 })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Your contacts (2)' })).toBeVisible()
  await expect(page.getByText('Your assistant can only send money to accounts on this list.')).toBeVisible()
  await expectTheme(page, testInfo)
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-list')

  await page.getByRole('button', { name: 'Add contact' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Name').fill('Sam')
  await dialog.getByLabel('Address').fill(SAM.toLowerCase())
  await expect(dialog.getByText('Saved as “sam”.')).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-add')

  await dialog.getByLabel('Address').press('Enter')
  await expect(dialog.getByRole('heading', { name: 'Check the address' })).toBeVisible()
  // The whole checksummed address, in groups of four for reading.
  await expect(dialog.locator('.mf-address-box')).toContainText(SAM)
  await expect(dialog.locator('.mf-full-address-groups > span')).toHaveCount(11)
  expect(posted).toEqual([])
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-review')

  // Saving takes the signed-in wallet's signature over the contact.
  await expect(dialog.getByRole('button', { name: 'Save contact' })).toBeDisabled()
  await connectWallet(page)
  await expect(dialog.getByText(/Your wallet will ask you to sign this contact/)).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-sign')
  await dialog.getByRole('button', { name: 'Save contact' }).click()
  await expect(page.getByText('sam saved. Your assistant can now pay them.')).toBeVisible()
  await expect(dialog).toBeHidden()
  expect(signed).toEqual([
    expect.objectContaining({ primaryType: 'AddContact', message: expect.objectContaining({ name: 'sam', nonce: 'nonce1' }) }),
  ])
  expect(posted).toEqual([{ name: 'sam', address: SAM, nonce: 'nonce1', signature: SIGNATURE }])
  await expect(page.getByRole('heading', { name: 'Your contacts (3)' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Remove sam' })).toBeVisible()
})

test('replacing an address and removing a contact both ask first', async ({ page }, testInfo) => {
  const { posted, deleted } = await mockServer(page, [
    { name: "o'neil & co", address: ALEX },
    { name: 'sam', address: SAM },
  ])
  await page.goto('/contacts')

  // Replace.
  await page.getByRole('button', { name: 'Add contact' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel('Name').fill('sam')
  await dialog.getByLabel('Address').fill(NEW)
  await expect(dialog.getByText('You already have a contact called sam.')).toBeVisible()
  await dialog.getByRole('button', { name: 'Continue' }).click()
  await expect(dialog.getByRole('heading', { name: "Replace sam's address?" })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-replace')
  await connectWallet(page)
  await dialog.getByRole('button', { name: 'Replace address' }).click()
  await expect(page.getByText("sam's address updated.")).toBeVisible()
  expect(posted).toEqual([{ name: 'sam', address: NEW, nonce: 'nonce1', signature: SIGNATURE }])

  // Remove, with a name that needs encoding in the URL.
  await page.getByRole('button', { name: "Remove o'neil & co" }).click()
  const confirm = page.getByRole('alertdialog')
  await expect(confirm).toContainText("won't be able to send money to o'neil & co any more")
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-remove')
  await confirm.getByRole('button', { name: 'Remove' }).click()
  await expect(page.getByText("o'neil & co removed.")).toBeVisible()
  expect(deleted).toEqual(["/api/contacts/o'neil%20%26%20co"])
  await expect(page.getByRole('heading', { name: 'Your contacts (1)' })).toBeVisible()
})

test('an empty list', async ({ page }, testInfo) => {
  await mockServer(page, [])
  await page.goto('/contacts')

  await expect(page.getByRole('heading', { name: 'No contacts yet' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add contact' })).toBeVisible()
  await expectNoSidewaysScroll(page)
  await snap(page, testInfo, 'contacts-empty')
})
