import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { resetClientForTests } from '../../api/client'
import type { ConnectedApp } from '../../api/types'
import { json, makeWagmiConfig, Providers, TOKEN } from '../../test/utils'
import { ConnectedAppsPanel } from './ConnectedAppsPanel'

const CLAUDE: ConnectedApp = {
  id: 3,
  app: { key: 'claude', name: 'Claude', site: 'claude.ai' },
  created_at: 1_790_000_000,
  last_used_at: 1_790_100_000,
}
const CHATGPT: ConnectedApp = {
  id: 4,
  app: { key: 'chatgpt', name: 'ChatGPT', site: 'chatgpt.com' },
  created_at: 1_790_200_000,
  last_used_at: null,
}

/** Signed in; /api/connections lists `apps` until one is deleted. Returns the deleted ids. */
function stubApi(apps: ConnectedApp[]) {
  let listed = [...apps]
  const deleted: number[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith('/api/auth/refresh')) return Promise.resolve(json(200, TOKEN))
      if (url.endsWith('/api/connections')) return Promise.resolve(json(200, { connections: listed }))
      const gone = /\/api\/connections\/(\d+)$/.exec(url)
      if (gone && init?.method === 'DELETE') {
        deleted.push(Number(gone[1]))
        listed = listed.filter(app => app.id !== Number(gone[1]))
        return Promise.resolve(new Response(null, { status: 204 }))
      }
      return Promise.resolve(json(404, { detail: 'Not Found' }))
    }),
  )
  return deleted
}

function renderPanel() {
  render(
    <Providers wagmiConfig={makeWagmiConfig()}>
      <ConnectedAppsPanel />
    </Providers>,
  )
}

describe('ConnectedAppsPanel', () => {
  beforeEach(() => resetClientForTests())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('lists the connected apps, and says where to connect one', async () => {
    stubApi([CHATGPT, CLAUDE])
    renderPanel()

    expect(await screen.findByRole('heading', { name: 'Claude' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'ChatGPT' })).toBeInTheDocument()
    expect(screen.getByText(/chatgpt\.com · Connected .* · not used yet/)).toBeInTheDocument()
    expect(screen.getByText(`${window.location.origin}/mcp`)).toBeInTheDocument()
  })

  it('disconnects an app, and it leaves the list', async () => {
    const deleted = stubApi([CHATGPT, CLAUDE])
    renderPanel()
    await screen.findByRole('heading', { name: 'Claude' })

    const [, claudeRow] = screen.getAllByRole('button', { name: 'Disconnect' })
    await userEvent.setup().click(claudeRow)

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Claude' })).toBeNull())
    expect(deleted).toEqual([3])
    expect(screen.getByRole('heading', { name: 'ChatGPT' })).toBeInTheDocument()
  })

  it('says so when no app is connected', async () => {
    stubApi([])
    renderPanel()

    expect(await screen.findByText('No apps connected.')).toBeInTheDocument()
  })
})
