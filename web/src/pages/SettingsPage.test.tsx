import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../api/client'
import { routes } from '../routes'
import { json, ME, renderRoutes, setViewportWidth, TOKEN } from '../test/utils'

/** A signed-in account; DELETE /api/chat/history answers `clearStatus`. */
function stubServer(clearStatus = 200) {
  const cleared: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (url === '/api/auth/refresh') return Promise.resolve(json(200, TOKEN))
      if (url === '/api/me') return Promise.resolve(json(200, ME))
      if (url.startsWith('/api/chat/history') && init?.method === 'DELETE') {
        cleared.push(url)
        return Promise.resolve(
          clearStatus === 200
            ? json(200, { status: 'cleared', chain_ids: [11155111, 56] })
            : json(clearStatus, { detail: 'The assistant is still answering a message.' }),
        )
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return { cleared }
}

describe('SettingsPage: chat history', () => {
  beforeEach(() => {
    resetClientForTests()
    setViewportWidth(1280)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('deletes every network’s chat after asking, and keeps the transaction history', async () => {
    const { cleared } = stubServer()
    const user = userEvent.setup()
    await renderRoutes(routes, '/settings')

    await user.click(await screen.findByRole('button', { name: 'Delete all' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/every network, including anything said on Telegram/)
    expect(dialog).toHaveTextContent(/transaction history stay as they are/)
    await user.click(within(dialog).getByRole('button', { name: 'Delete all' }))

    expect(await screen.findByText('All chat history deleted.')).toBeInTheDocument()
    expect(cleared).toEqual(['/api/chat/history'])
  })

  it('says so when the assistant is still answering', async () => {
    stubServer(409)
    const user = userEvent.setup()
    await renderRoutes(routes, '/settings')

    await user.click(await screen.findByRole('button', { name: 'Delete all' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete all' }))
    await waitFor(() => expect(screen.getByText(/still answering a message/)).toBeInTheDocument())
  })
})
