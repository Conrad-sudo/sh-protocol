import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Logo } from '../components/brand/Logo'
import { ThemeSwitch, ThemeToggleButton } from '../components/ThemeSwitch'
import { ThemeProvider } from './ThemeProvider'
import { STORAGE_KEY } from './themeStore'

function renderThemed() {
  return render(
    <ThemeProvider>
      <Logo />
      <ThemeSwitch />
      <ThemeToggleButton />
    </ThemeProvider>,
  )
}

const markSrc = () => document.querySelector('.mf-logo img')?.getAttribute('src')

describe('ThemeProvider', () => {
  afterEach(() => {
    cleanup()
    localStorage.clear()
    document.body.className = ''
    delete (window as { prefersDark?: boolean }).prefersDark
  })

  it('follows the system preference by default', () => {
    ;(window as { prefersDark?: boolean }).prefersDark = true
    renderThemed()
    expect(document.body).toHaveClass('rs-theme-dark')
    expect(markSrc()).toBe('/brand/mark-dark.png')
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('remembers an explicit choice, and forgets it on "System"', async () => {
    const user = userEvent.setup()
    renderThemed()
    expect(document.body).toHaveClass('rs-theme-light')
    expect(markSrc()).toBe('/brand/mark-light.png')

    await user.click(screen.getByText('Dark'))
    expect(document.body).toHaveClass('rs-theme-dark')
    expect(document.body).not.toHaveClass('rs-theme-light')
    expect(localStorage.getItem(STORAGE_KEY)).toBe('dark')
    expect(markSrc()).toBe('/brand/mark-dark.png')

    await user.click(screen.getByText('System'))
    expect(document.body).toHaveClass('rs-theme-light')
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('flips with the one-tap toggle', async () => {
    renderThemed()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Switch to dark theme' }))
    expect(document.body).toHaveClass('rs-theme-dark')
    expect(screen.getByRole('button', { name: 'Switch to light theme' })).toBeInTheDocument()
  })

  it('starts from a stored choice', () => {
    localStorage.setItem(STORAGE_KEY, 'dark')
    renderThemed()
    expect(document.body).toHaveClass('rs-theme-dark')
  })
})
