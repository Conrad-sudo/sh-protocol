import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import App from '../App'
import { ThemeProvider } from './ThemeProvider'
import { STORAGE_KEY } from './themeStore'

function renderApp() {
  return render(
    <ThemeProvider>
      <App />
    </ThemeProvider>,
  )
}

describe('ThemeProvider', () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    document.body.className = ''
    delete (window as { prefersDark?: boolean }).prefersDark
  })

  it('follows the system preference by default', () => {
    ;(window as { prefersDark?: boolean }).prefersDark = true
    renderApp()
    expect(document.body).toHaveClass('rs-theme-dark')
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('remembers an explicit choice, and forgets it on "System"', async () => {
    const user = userEvent.setup()
    renderApp()
    expect(document.body).toHaveClass('rs-theme-light')

    await user.click(screen.getByText('Dark'))
    expect(document.body).toHaveClass('rs-theme-dark')
    expect(document.body).not.toHaveClass('rs-theme-light')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('dark')
    // The mark is decorative (alt=""), so it has no img role to query by.
    expect(document.querySelector('main img')).toHaveAttribute('src', '/brand/mark-dark.png')

    await user.click(screen.getByText('System'))
    expect(document.body).toHaveClass('rs-theme-light')
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('starts from a stored choice', () => {
    localStorage.setItem(STORAGE_KEY, 'dark')
    renderApp()
    expect(document.body).toHaveClass('rs-theme-dark')
  })
})
