// Turns Key-logo.png into the two marks in public/brand/ and the app icons in public/. Run with
// `npm run marks`.
//
// The artwork is drawn on white paper, so the first job is to knock the paper out to alpha —
// otherwise the ring would sit on a white disc on a navy sidebar. The second is the dark copy:
// the same shape with the navy ring and green traces swapped for their on-dark values and the
// black key inverted to white. Both run in a browser canvas, so no image library is needed.
//
// The icons are the key's handle alone — the ring and its circuit, cut from the same artwork so the
// circuitry is the logo's own rather than a redrawing of it. The key is dropped: its shaft crosses
// the ring at the lower left, which leaves a short break in the ring there, as in the logo.
import { chromium } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

/** Mark height in the output files. Nothing shows the mark above 64px, so this is plenty. */
const HEIGHT = 384
const RING_ON_DARK = [127, 176, 230] // #7FB0E6, the pale blue ring on navy
const WIRE_ON_DARK = [43, 183, 126] // #2BB77E, the brand green on dark
const NAVY = [6, 42, 78] // #062A4E, --mf-navy and the manifest's background colour
/** Share of an app icon the handle spans. Keeps the ring inside a maskable icon's safe circle. */
const ICON_FILL = 0.76
/** Pixel size of each copy of the handle inside favicon.svg: 32px tabs at 2x. */
const FAVICON_SVG_SIZE = 64

// Handed to the page as a data URL: a file:// image on about:blank cannot be decoded, and one
// loaded cross-origin would taint the canvas and block getImageData.
const png = await readFile(fileURLToPath(new URL('./Key-logo.png', import.meta.url)))
const source = `data:image/png;base64,${png.toString('base64')}`
const publicDir = (name: string) => fileURLToPath(new URL(`../../public/${name}`, import.meta.url))

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto('about:blank')

const images = await page.evaluate(
  async ([src, height, ringOnDark, wireOnDark, navy, iconFill, faviconSize]: [
    string,
    number,
    number[],
    number[],
    number[],
    number,
    number,
  ]) => {
    const img = new Image()
    img.src = src
    await img.decode()

    const read = document.createElement('canvas')
    read.width = img.naturalWidth
    read.height = img.naturalHeight
    const rc = read.getContext('2d')!
    rc.drawImage(img, 0, 0)
    const { data, width, height: h } = rc.getImageData(0, 0, read.width, read.height)

    type Kind = 'ring' | 'wire' | 'key'
    /** One source pixel with the paper knocked out, or null where it was only paper. */
    const pixel = (x: number, y: number): { rgb: number[]; alpha: number; kind: Kind } | null => {
      const i = (y * width + x) * 4
      const [r, g, b, a] = [data[i], data[i + 1], data[i + 2], data[i + 3]]

      // White to alpha: keep the colour, drop the paper it was printed on.
      const min = Math.min(r, g, b)
      const alpha = a * (1 - min / 255)
      if (alpha < 0.5) return null
      const scale = 255 / (255 - min)
      const rgb = [r, g, b].map(c => Math.min(255, Math.max(0, 255 - (255 - c) * scale)))

      // The anti-aliasing now lives in the alpha, so each colour can be assigned flat.
      const [R, G, B] = rgb
      const sat = Math.max(R, G, B) - Math.min(R, G, B)
      const ring = sat >= 30 && B >= G && B - R > 20
      const wire = sat >= 30 && !ring && G >= B
      return { rgb, alpha, kind: ring ? 'ring' : wire ? 'wire' : 'key' }
    }

    // The ink is everything opaque that is not near-white: that box is the mark.
    let x0 = width
    let y0 = h
    let x1 = -1
    let y1 = -1
    // The ring's own box locates the handle for the icons.
    let rx0 = width
    let ry0 = h
    let rx1 = -1
    let ry1 = -1
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < width; x++) {
        const i = (y * width + x) * 4
        const white = data[i] > 235 && data[i + 1] > 235 && data[i + 2] > 235
        if (data[i + 3] > 40 && !white) {
          if (x < x0) x0 = x
          if (x > x1) x1 = x
          if (y < y0) y0 = y
          if (y > y1) y1 = y
          if (pixel(x, y)?.kind === 'ring') {
            if (x < rx0) rx0 = x
            if (x > rx1) rx1 = x
            if (y < ry0) ry0 = y
            if (y > ry1) ry1 = y
          }
        }
      }
    }
    const bw = x1 - x0 + 1
    const bh = y1 - y0 + 1

    const light = new ImageData(bw, bh)
    const dark = new ImageData(bw, bh)
    for (let y = 0; y < bh; y++) {
      for (let x = 0; x < bw; x++) {
        const p = pixel(x + x0, y + y0)
        if (!p) continue
        const o = (y * bw + x) * 4
        light.data.set([...p.rgb, p.alpha], o)
        const on = p.kind === 'ring' ? ringOnDark : p.kind === 'wire' ? wireOnDark : p.rgb.map(c => 255 - c)
        dark.data.set([...on, p.alpha], o)
      }
    }

    // The handle: a square around the ring, keeping only ring and circuit — the key's shaft, which
    // crosses the ring, is left out.
    const side = Math.max(rx1 - rx0, ry1 - ry0) + 5
    const hx = Math.round((rx0 + rx1 - side) / 2)
    const hy = Math.round((ry0 + ry1 - side) / 2)
    const handleLight = new ImageData(side, side)
    const handleDark = new ImageData(side, side)
    for (let y = 0; y < side; y++) {
      for (let x = 0; x < side; x++) {
        const sx = x + hx
        const sy = y + hy
        if (sx < 0 || sy < 0 || sx >= width || sy >= h) continue
        const p = pixel(sx, sy)
        if (!p || p.kind === 'key') continue
        const o = (y * side + x) * 4
        handleLight.data.set([...p.rgb, p.alpha], o)
        handleDark.data.set([...(p.kind === 'ring' ? ringOnDark : wireOnDark), p.alpha], o)
      }
    }

    const canvasOf = (pixels: ImageData) => {
      const c = document.createElement('canvas')
      c.width = pixels.width
      c.height = pixels.height
      c.getContext('2d')!.putImageData(pixels, 0, 0)
      return c
    }
    // Halving until close, then one last step: a single big reduction skips source pixels, and at
    // favicon size that drops whole traces.
    const scaled = (from: HTMLCanvasElement, w: number, hh: number) => {
      let cur = from
      while (cur.width / 2 >= w && cur.height / 2 >= hh) {
        const half = document.createElement('canvas')
        half.width = Math.round(cur.width / 2)
        half.height = Math.round(cur.height / 2)
        const hc = half.getContext('2d')!
        hc.imageSmoothingQuality = 'high'
        hc.drawImage(cur, 0, 0, half.width, half.height)
        cur = half
      }
      const out = document.createElement('canvas')
      out.width = w
      out.height = hh
      const oc = out.getContext('2d')!
      oc.imageSmoothingQuality = 'high'
      oc.drawImage(cur, 0, 0, w, hh)
      return out
    }
    const base64 = (c: HTMLCanvasElement) => c.toDataURL('image/png').split(',')[1]

    const markWidth = Math.round((bw / bh) * height)
    const lightHandle = canvasOf(handleLight)
    const darkHandle = canvasOf(handleDark)

    /** The handle on a navy square, for the home-screen and install icons. */
    const icon = (size: number) => {
      const c = document.createElement('canvas')
      c.width = size
      c.height = size
      const cc = c.getContext('2d')!
      cc.fillStyle = `rgb(${navy.join(',')})`
      cc.fillRect(0, 0, size, size)
      const d = Math.round(size * iconFill)
      cc.drawImage(scaled(darkHandle, d, d), Math.round((size - d) / 2), Math.round((size - d) / 2))
      return base64(c)
    }

    /** The marks' original single-step reduction, so re-running the script leaves them unchanged. */
    const mark = (pixels: ImageData) => {
      const small = document.createElement('canvas')
      small.width = markWidth
      small.height = height
      const sc = small.getContext('2d')!
      sc.imageSmoothingQuality = 'high'
      sc.drawImage(canvasOf(pixels), 0, 0, markWidth, height)
      return base64(small)
    }

    return {
      markLight: mark(light),
      markDark: mark(dark),
      markWidth,
      icon512: icon(512),
      icon192: icon(192),
      appleTouch: icon(180),
      favicon32: base64(scaled(lightHandle, 32, 32)),
      faviconLight: base64(scaled(lightHandle, faviconSize, faviconSize)),
      faviconDark: base64(scaled(darkHandle, faviconSize, faviconSize)),
    }
  },
  [source, HEIGHT, RING_ON_DARK, WIRE_ON_DARK, NAVY, ICON_FILL, FAVICON_SVG_SIZE] as [
    string,
    number,
    number[],
    number[],
    number[],
    number,
    number,
  ],
)

await browser.close()

const png64 = (b64: string) => Buffer.from(b64, 'base64')
await writeFile(publicDir('brand/mark-light.png'), png64(images.markLight))
await writeFile(publicDir('brand/mark-dark.png'), png64(images.markDark))
await writeFile(publicDir('icon-512.png'), png64(images.icon512))
await writeFile(publicDir('icon-192.png'), png64(images.icon192))
await writeFile(publicDir('apple-touch-icon.png'), png64(images.appleTouch))
await writeFile(publicDir('favicon-32.png'), png64(images.favicon32))

// The tab icon swaps with the browser's colour scheme, as the marks do: a navy ring disappears on
// a dark tab strip. SVG favicons honour prefers-color-scheme; the two copies are the handle itself.
const S = FAVICON_SVG_SIZE
await writeFile(
  publicDir('favicon.svg'),
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${S} ${S}">
<style>.dark{display:none}@media (prefers-color-scheme: dark){.light{display:none}.dark{display:inline}}</style>
<image class="light" width="${S}" height="${S}" href="data:image/png;base64,${images.faviconLight}"/>
<image class="dark" width="${S}" height="${S}" href="data:image/png;base64,${images.faviconDark}"/>
</svg>
`,
)

console.log(
  `Wrote mark-light.png and mark-dark.png at ${images.markWidth}×${HEIGHT}, plus favicon.svg, ` +
    `favicon-32.png, apple-touch-icon.png, icon-192.png and icon-512.png. ` +
    `Logo.tsx's MARK_RATIO must match ${images.markWidth} / ${HEIGHT}.`,
)
