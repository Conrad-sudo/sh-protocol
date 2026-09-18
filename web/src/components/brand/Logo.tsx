import { useTheme } from '../../theme/useTheme'

// Intrinsic size of public/brand/mark-*.png, for the width/height the browser reserves.
const MARK_RATIO = 348 / 384

interface LogoProps {
  /** `mark` is the key alone; `lockup` adds the "mitfah" wordmark. */
  variant?: 'mark' | 'lockup'
  /** Mark height in px. */
  size?: number
  /** `onDark` forces the white-key mark, for the navy sidebar in either theme. */
  tone?: 'auto' | 'onDark'
  className?: string
}

export function Logo({ variant = 'lockup', size = 28, tone = 'auto', className }: LogoProps) {
  const { resolved } = useTheme()
  const dark = tone === 'onDark' || resolved === 'dark'

  const mark = (
    <img
      src={dark ? '/brand/mark-dark.png' : '/brand/mark-light.png'}
      alt=""
      height={size}
      width={Math.round(size * MARK_RATIO)}
    />
  )

  return (
    <span
      className={['mf-logo', className].filter(Boolean).join(' ')}
      data-tone={dark ? 'dark' : 'light'}
      {...(variant === 'mark' ? { role: 'img', 'aria-label': 'Mitfah' } : {})}
    >
      {mark}
      {variant === 'lockup' && (
        <span className="mf-logo-word" style={{ fontSize: Math.round(size * 0.82) }}>
          mitfah
        </span>
      )}
    </span>
  )
}
