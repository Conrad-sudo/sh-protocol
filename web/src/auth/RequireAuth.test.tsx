import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, screen, waitFor } from '@testing-library/react'
import { resetClientForTests } from '../api/client'
import { json, ME, renderRoutes, TOKEN } from '../test/utils'
import { RequireAuth } from './RequireAuth'

const routes = [
  { path: '/login', element: <p>login page</p> },
  {
    path: '/contacts',
    element: (
      <RequireAuth>
        <p>contacts page</p>
      </RequireAuth>
    ),
  },
]

describe('RequireAuth', () => {
  beforeEach(() => resetClientForTests())
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('sends a signed-out visitor to /login, remembering where they were going', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(json(401, { detail: 'No refresh token' }))))
    const router = await renderRoutes(routes, '/contacts?tab=all')

    expect(await screen.findByText('login page')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/contacts?tab=all')}`)
  })

  it('shows the page once the session is restored from the cookie', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => Promise.resolve(url.endsWith('/refresh') ? json(200, TOKEN) : json(200, ME))),
    )
    await renderRoutes(routes, '/contacts')

    await waitFor(() => expect(screen.getByText('contacts page')).toBeInTheDocument())
  })
})
