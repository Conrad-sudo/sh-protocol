import { LiquidGlass } from 'quick-liquid/react'
import { type ComponentProps, useContext } from 'react'
import { Panel, type PanelProps } from 'rsuite'
import { ThemeContext } from '../theme/ThemeContext'

/*
 * Liquid glass (quick-liquid): the surface bends the wallpaper behind it through a curved rim,
 * frosts it, tints it with the plate colour and catches the light on its edge. Only Chromium
 * browsers bend; Safari and Firefox get the frost, tint and rim light.
 *
 * The glass is a layer BEHIND a surface's content, not a wrapper round it. The engine clips the
 * element it draws on (overflow: hidden) and sets its corners and shadow inline, which on the
 * content itself would cut off the top bar's dropdown menus and the focus rings at a card's edge.
 * So a surface carries the class `mf-glass-surface`, this layer is its first child, and the content
 * after it paints on top (styles/app.css, "Liquid glass").
 */

/**
 * Corners, in px. `plate` is --mf-radius-plate in tokens.css. The engine keeps a corner to half the
 * shorter side, so `capsule` rounds a bar's ends and `disc` makes a square a circle.
 */
const RADIUS = { plate: 28, capsule: 999, disc: 999 }

/** The plate colour (--mf-plate) in each theme, laid over the glass at 40%. */
const TINT = { light: '250, 251, 252', dark: '13, 26, 43' }

// The engine watches its element with a ResizeObserver, which jsdom (the unit tests) doesn't have.
const SUPPORTED = typeof ResizeObserver !== 'undefined'

export function GlassLayer({ shape = 'plate' }: { shape?: keyof typeof RADIUS }) {
  // The app's theme, not the system's: the theme switch can override the system.
  const theme = useContext(ThemeContext)?.resolved ?? 'light'

  return (
    <LiquidGlass
      aria-hidden
      className="mf-glass"
      active={SUPPORTED}
      config={{
        appearance: theme,
        tint: TINT[theme],
        tintOpacity: 0.4,
        blur: 12,
        saturation: 1.4,
        // The lens: a 30px curved rim that shifts the wallpaper up to 18px near the edge. The
        // content on the glass is never bent, only what is behind it.
        bezelWidth: 30,
        refractionStrength: 18,
        thickness: 20,
        // Colour fringes would be blurred away by the frost anyway; one displacement pass, not three.
        chromaticAberration: 0,
        borderRadius: RADIUS[shape],
        // The lens map is smooth, so large plates don't need it at full size.
        quality: shape === 'capsule' ? 'high' : 'medium',
      }}
    />
  )
}

/** An RSuite Panel drawn as a glass plate. */
export function GlassPanel({ className, ...props }: PanelProps) {
  return <Panel {...props} as={GlassPlate} className={className ? `mf-glass-surface ${className}` : 'mf-glass-surface'} />
}

function GlassPlate({ children, ...props }: ComponentProps<'div'>) {
  return (
    <div {...props}>
      <GlassLayer />
      {children}
    </div>
  )
}
