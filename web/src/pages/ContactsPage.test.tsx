import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { getAddress } from 'viem'
import { resetClientForTests } from '../api/client'
import type { Contact } from '../api/types'
import { routes } from '../routes'
import { json, ME, renderRoutes, setViewportWidth, TOKEN } from '../test/utils'

const SAM = '0x70997970C51812dc3A010C7d01b50e0d17dc79C8'
const ALEX = '0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC'
const NEW = '0x90F79bf6EB2c4f870365E785982E1f101E93b906'

interface ServerOptions {
  contacts?: Contact[]
  /** GET /api/contacts fails this many times first. */
  listFailures?: number
  /** Every GET /api/contacts after the first fails. */
  listFailsLater?: boolean
  /** POST /api/contacts waits for this before answering. */
  saveGate?: Promise<void>
  /** POST /api/contacts answers with this error. */
  saveError?: { status: number; detail: string }
  /** DELETE answers 404 (removed elsewhere, so gone) or 500 (still there). */
  deleteStatus?: 404 | 500
}

/** What an unhandled server error looks like: plain text, not JSON. */
function serverError() {
  return new Response('Internal Server Error', { status: 500 })
}

/** A signed-in account with the contacts API, behaving like app/api.py. */
function stubServer({
  contacts = [],
  listFailures = 0,
  listFailsLater = false,
  saveGate = Promise.resolve(),
  saveError,
  deleteStatus,
}: ServerOptions = {}) {
  let saved = [...contacts]
  let lists = 0
  const posted: unknown[] = []
  const deleted: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, ME))
      if (url === '/api/contacts' && method === 'GET') {
        lists++
        if (listFailures-- > 0 || (listFailsLater && lists > 1)) return Promise.resolve(serverError())
        return Promise.resolve(json(200, { contacts: [...saved].sort((a, b) => (a.name < b.name ? -1 : 1)) }))
      }
      if (url === '/api/contacts' && method === 'POST') {
        const body = JSON.parse(String(init?.body)) as Contact
        posted.push(body)
        return saveGate.then(() => {
          if (saveError) return json(saveError.status, { detail: saveError.detail })
          const contact = { name: body.name.trim().toLowerCase(), address: getAddress(body.address) }
          saved = [...saved.filter(c => c.name !== contact.name), contact]
          return json(201, contact)
        })
      }
      if (url.startsWith('/api/contacts/') && method === 'DELETE') {
        deleted.push(url)
        const name = decodeURIComponent(url.slice('/api/contacts/'.length))
        if (deleteStatus === 404) {
          saved = saved.filter(c => c.name !== name)
          return Promise.resolve(json(404, { detail: `'${name}' is not a saved contact` }))
        }
        if (deleteStatus) return Promise.resolve(serverError())
        saved = saved.filter(c => c.name !== name)
        return Promise.resolve(json(200, { status: 'deleted', name }))
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { posted, deleted }
}

async function openAddDialog(user: UserEvent) {
  await user.click(await screen.findByRole('button', { name: 'Add contact' }))
  return screen.findByRole('dialog', { name: 'Add a contact' })
}

async function fillContact(user: UserEvent, dialog: HTMLElement, name: string, address: string) {
  if (name) await user.type(within(dialog).getByLabelText('Name'), name)
  if (address) await user.type(within(dialog).getByLabelText('Address'), address)
}

function rows() {
  return [...document.querySelectorAll('.mf-contact')].map(row => row.getAttribute('data-contact'))
}

describe('ContactsPage', () => {
  beforeEach(() => {
    resetClientForTests()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('lists the contacts, and says what the list is for', async () => {
    stubServer({ contacts: [{ name: 'sam', address: SAM }, { name: 'alex', address: ALEX }] })
    await renderRoutes(routes, '/contacts')

    expect(await screen.findByRole('heading', { name: 'Your contacts (2)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Contacts', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('Your assistant can only send money to people on this list.')).toBeInTheDocument()
    expect(rows()).toEqual(['alex', 'sam'])
    const sam = document.querySelector<HTMLElement>('[data-contact="sam"]')!
    expect(within(sam).getByText(SAM)).toBeInTheDocument()
    expect(within(sam).getByRole('button', { name: 'Copy address of sam' })).toBeInTheDocument()
    expect(within(sam).getByRole('button', { name: 'Remove sam' })).toBeInTheDocument()
  })

  it('invites the first contact when there are none', async () => {
    stubServer()
    await renderRoutes(routes, '/contacts')

    expect(await screen.findByRole('heading', { name: 'No contacts yet' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Your contacts (0)' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add contact' })).toBeInTheDocument()
  })

  it('offers a retry when the list fails to load', async () => {
    stubServer({ contacts: [{ name: 'sam', address: SAM }], listFailures: 1 })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    expect(await screen.findByText(/Couldn't load your contacts: Something went wrong on our side/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('heading', { name: 'Your contacts (1)' })).toBeInTheDocument()
  })

  it('adds a contact after a look at the whole address', async () => {
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    expect(within(dialog).getByRole('button', { name: 'Continue' })).toBeDisabled()
    await fillContact(user, dialog, 'Sam', SAM.toLowerCase())
    expect(within(dialog).getByText('Saved as “sam”.')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))

    // The second look: the checksummed address in full, and nothing sent yet.
    expect(within(dialog).getByRole('heading', { name: 'Check the address' })).toBeInTheDocument()
    expect(within(dialog).getByText('Your assistant will be able to send money to sam at:')).toBeInTheDocument()
    expect(within(dialog).getByText(SAM)).toBeInTheDocument()
    expect(posted).toEqual([])

    // Back keeps what was typed.
    await user.click(within(dialog).getByRole('button', { name: 'Back' }))
    expect(within(dialog).getByLabelText('Name')).toHaveValue('Sam')
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))

    await user.click(within(dialog).getByRole('button', { name: 'Save contact' }))
    expect(await screen.findByText('sam saved. Your assistant can now pay them.')).toBeInTheDocument()
    expect(posted).toEqual([{ name: 'sam', address: SAM }])
    await waitFor(() => expect(rows()).toEqual(['sam']))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Check the address' })).toBeNull())

    // Opening it again starts from a blank form.
    const again = await openAddDialog(user)
    expect(within(again).getByLabelText('Name')).toHaveValue('')
  })

  it('keeps showing the list when a refresh fails', async () => {
    stubServer({ listFailsLater: true })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, 'sam', SAM)
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save contact' }))

    // Saving refreshes the list, and that refresh fails.
    expect(await screen.findByText('sam saved. Your assistant can now pay them.')).toBeInTheDocument()
    await waitFor(() => expect(rows()).toEqual(['sam']))
    expect(screen.queryByText(/Couldn't load your contacts/)).toBeNull()
  })

  it("doesn't let a slow save close the dialog opened after it", async () => {
    let answer = () => {}
    const { posted } = stubServer({ saveGate: new Promise<void>(resolve => (answer = resolve)) })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, 'sam', SAM)
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save contact' }))
    await waitFor(() => expect(posted).toHaveLength(1))
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())

    const next = await openAddDialog(user)
    await user.type(within(next).getByLabelText('Name'), 'alex')
    answer()

    expect(await screen.findByText('sam saved. Your assistant can now pay them.')).toBeInTheDocument()
    await waitFor(() => expect(rows()).toEqual(['sam']))
    // The new dialog stays open (a closing one lingers while it animates), with what was typed.
    await expect(
      waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 1000 }),
    ).rejects.toThrow()
    expect(within(next).getByLabelText('Name')).toHaveValue('alex')
  })

  it.each([
    ['me', SAM, '"me" always means your own wallet, so it can\'t name a contact.'],
    ['a/b', SAM, 'A name can\'t contain "/", "\\" or tabs.'],
    ['..', SAM, "A name can't be just dots."],
    ['sam', '0x1234', 'An address is 0x and 40 more characters. This one has 4.'],
    ['sam', 'sam.eth', "Paste the 0x address. Names such as sam.eth aren't supported."],
    ['sam', SAM.replace('C5', 'c5'), "This address has a typo: its capital letters don't match. Copy it again from where you got it."],
    ['sam', `0x${'0'.repeat(40)}`, 'This is the zero address. Money sent there is lost for good.'],
  ])('refuses the name %j with the address %s', async (name, address, problem) => {
    const { posted } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, name, address)
    expect(within(dialog).getByText(problem)).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Continue' })).toBeDisabled()
    await user.keyboard('{Enter}')
    expect(within(dialog).getByRole('heading', { name: 'Add a contact' })).toBeInTheDocument()
    expect(posted).toEqual([])
  })

  it('asks before replacing the address of a name already saved', async () => {
    const { posted } = stubServer({ contacts: [{ name: 'sam', address: SAM }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, 'SAM', NEW)
    expect(
      within(dialog).getByText('You already have a contact called sam. Continuing replaces their address.'),
    ).toBeInTheDocument()
    // Enter continues too.
    await user.keyboard('{Enter}')

    expect(within(dialog).getByRole('heading', { name: "Replace sam's address?" })).toBeInTheDocument()
    const compare = dialog.querySelector<HTMLElement>('.mf-address-compare')!
    expect(within(compare).getByText(SAM)).toBeInTheDocument()
    expect(within(compare).getByText(NEW)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Replace address' }))

    expect(await screen.findByText("sam's address updated.")).toBeInTheDocument()
    expect(posted).toEqual([{ name: 'sam', address: NEW }])
    const sam = document.querySelector<HTMLElement>('[data-contact="sam"]')!
    await waitFor(() => expect(within(sam).getByText(NEW)).toBeInTheDocument())
  })

  it('says when a save would change nothing', async () => {
    stubServer({ contacts: [{ name: 'sam', address: SAM }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, 'sam', SAM.toLowerCase())
    expect(within(dialog).getByText('sam already has this address.')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Continue' })).toBeDisabled()
  })

  it('points out an address saved under another name', async () => {
    stubServer({ contacts: [{ name: 'sam', address: SAM }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, 'sammy', SAM)
    expect(within(dialog).getByText('Already saved as sam.')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Continue' })).toBeEnabled()
  })

  it('shows why a save failed, and keeps the dialog open', async () => {
    stubServer({ saveError: { status: 422, detail: "'me' is reserved — it always refers to your own wallet" } })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    const dialog = await openAddDialog(user)
    await fillContact(user, dialog, 'sam', SAM)
    await user.click(within(dialog).getByRole('button', { name: 'Continue' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save contact' }))

    expect(
      await within(dialog).findByText("Couldn't save: 'me' is reserved — it always refers to your own wallet"),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Save contact' })).toBeEnabled()
    expect(rows()).toEqual([])
  })

  it('removes a contact after asking, by its encoded name', async () => {
    const { deleted } = stubServer({
      contacts: [
        { name: "o'neil & co?", address: ALEX },
        { name: 'sam', address: SAM },
      ],
    })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    await user.click(await screen.findByRole('button', { name: "Remove o'neil & co?" }))
    const confirm = await screen.findByRole('alertdialog', { name: "Remove o'neil & co??" })
    expect(within(confirm).getByText(/won't be able to send money to o'neil & co\?/)).toBeInTheDocument()
    expect(deleted).toEqual([])
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }))

    expect(await screen.findByText("o'neil & co? removed.")).toBeInTheDocument()
    expect(deleted).toEqual(["/api/contacts/o'neil%20%26%20co%3F"])
    await waitFor(() => expect(rows()).toEqual(['sam']))
    expect(screen.getByRole('heading', { name: 'Your contacts (1)' })).toBeInTheDocument()
  })

  it('keeps a contact when cancelling the removal', async () => {
    const { deleted } = stubServer({ contacts: [{ name: 'sam', address: SAM }] })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    await user.click(await screen.findByRole('button', { name: 'Remove sam' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(deleted).toEqual([])
    expect(rows()).toEqual(['sam'])
  })

  it('treats a contact already removed elsewhere as removed', async () => {
    stubServer({ contacts: [{ name: 'sam', address: SAM }], deleteStatus: 404 })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    await user.click(await screen.findByRole('button', { name: 'Remove sam' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove' }))
    expect(await screen.findByText('sam was already removed.')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'No contacts yet' })).toBeInTheDocument()
  })

  it('keeps the contact and says why when removing fails', async () => {
    stubServer({ contacts: [{ name: 'sam', address: SAM }], deleteStatus: 500 })
    const user = userEvent.setup()
    await renderRoutes(routes, '/contacts')

    await user.click(await screen.findByRole('button', { name: 'Remove sam' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove' }))
    expect(await screen.findByText("Couldn't remove sam: Something went wrong on our side. Please try again.")).toBeInTheDocument()
    expect(rows()).toEqual(['sam'])
  })
})
