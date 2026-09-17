import { expect, test } from '@playwright/test'

/*
 * Contacts for real, against the running API and its database: a fresh account adds someone,
 * replaces their address and removes a contact whose name needs encoding in the URL, and the API's
 * own list agrees after each step. Nothing touches a chain.
 * Run with: E2E_REAL=1 npx playwright test real/contacts --project=desktop-light
 */

const SAM = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const NEW = '0x90F79bf6EB2c4f870365E785982E1f101E93b906'
const ODD = "o'neil & co? #1"

test('contacts are saved, replaced and removed through the real API', async ({ page, request }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop-light', 'One real run per suite is enough.')
  const email = `e2e-contacts-${Date.now()}@example.com`
  const password = 'Contacts-e2e-2026'

  await page.goto('/signup?next=%2Fcontacts')
  await page.getByLabel('Email').fill(email)
  await page.locator('input[name="password"]').fill(password)
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByRole('heading', { name: 'No contacts yet' })).toBeVisible()

  const login = await request.post('/api/auth/login', { data: { email, password } })
  expect(login.ok()).toBeTruthy()
  const { access_token } = (await login.json()) as { access_token: string }
  const headers = { Authorization: `Bearer ${access_token}` }
  const saved = async () => {
    const response = await request.get('/api/contacts', { headers })
    return ((await response.json()) as { contacts: unknown[] }).contacts
  }

  // The server refuses the names the page refuses: "me" means the wallet itself, and a browser
  // could never send the request to delete "." or "..".
  for (const name of ['me', '.', '..']) {
    const response = await request.post('/api/contacts', { headers, data: { name, address: SAM } })
    expect(response.status(), `saving ${name}`).toBe(422)
  }

  const add = async (name: string, address: string, confirmLabel: string) => {
    await page.getByRole('button', { name: 'Add contact' }).click()
    const dialog = page.getByRole('dialog')
    await dialog.getByLabel('Name').fill(name)
    await dialog.getByLabel('Address').fill(address)
    await dialog.getByRole('button', { name: 'Continue' }).click()
    await dialog.getByRole('button', { name: confirmLabel }).click()
    await expect(dialog).toBeHidden()
  }

  // Typed in capitals and lowercase hex; stored lowercase and checksummed.
  await add('Sam', SAM.toLowerCase(), 'Save contact')
  await expect(page.getByText('sam saved. Your assistant can now pay them.')).toBeVisible()
  expect(await saved()).toEqual([{ name: 'sam', address: SAM }])

  await add('SAM', NEW, 'Replace address')
  await expect(page.getByText("sam's address updated.")).toBeVisible()
  expect(await saved()).toEqual([{ name: 'sam', address: NEW }])

  await add(ODD, SAM, 'Save contact')
  await expect(page.getByRole('heading', { name: 'Your contacts (2)' })).toBeVisible()
  expect(await saved()).toContainEqual({ name: ODD, address: SAM })

  await page.getByRole('button', { name: `Remove ${ODD}` }).click()
  await page.getByRole('alertdialog').getByRole('button', { name: 'Remove' }).click()
  await expect(page.getByText(`${ODD} removed.`)).toBeVisible()
  expect(await saved()).toEqual([{ name: 'sam', address: NEW }])
  await expect(page.getByRole('heading', { name: 'Your contacts (1)' })).toBeVisible()
})
