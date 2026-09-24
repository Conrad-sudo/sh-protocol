// Turns Key-logo.png into the two marks in public/brand/ and the app icons in public/. Run with
// `npm run marks`.
//
// The artwork is a flat drawing on white paper: a navy ring, a green circuit and a black key. It
// is rendered as the objects it shows, lit from the upper left, so the logo keeps its exact shapes:
//   ring     an anodized blue bezel, bevelled, with the fine concentric rings of a turned part
//   circuit  vivid green traces standing proud of a dark circuit board set inside the ring
//   key      polished steel, brushed along the shaft, casting a short shadow on what it crosses
// Each part's bevel comes from its own outline: the mask is blurred into a height map, the height
// map gives a surface normal, and the normal is lit (diffuse, a sky reflection, a specular glint).
// Everything runs in a browser canvas, so no image library is needed.
//
// The icons are the key's handle alone — the ring, its board and its circuit — cut from the same
// render. The key is dropped: its shaft crosses the ring at the lower left.
import { chromium } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

/**
 * Mark height in the output files: 2x the largest the mark is ever shown (64px; today 40px at most).
 * Anything more is wasted download.
 */
const HEIGHT = 128
const NAVY = [6, 42, 78] // #062A4E, --mf-navy and the manifest's background colour
/** Share of an app icon the handle spans. Keeps the ring inside a maskable icon's safe circle. */
const ICON_FILL = 0.76
/** Pixel size of each copy of the handle inside favicon.svg: 32px tabs at 2x. */
const FAVICON_SVG_SIZE = 64
/** Set LOGO_PREVIEW to a directory to also write the marks and the icon at a large size there. */
const PREVIEW_DIR = process.env.LOGO_PREVIEW
const PREVIEW_HEIGHT = 720

// Handed to the page as a data URL: a file:// image on about:blank cannot be decoded, and one
// loaded cross-origin would taint the canvas and block getImageData.
const png = await readFile(fileURLToPath(new URL('./Key-logo.png', import.meta.url)))
const source = `data:image/png;base64,${png.toString('base64')}`
const publicDir = (name: string) => fileURLToPath(new URL(`../../public/${name}`, import.meta.url))

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto('about:blank')

const images = await page.evaluate(
  async ([src, height, navy, iconFill, faviconSize, previewHeight]: [string, number, number[], number, number, number]) => {
    const img = new Image()
    img.src = src
    await img.decode()

    const read = document.createElement('canvas')
    read.width = img.naturalWidth
    read.height = img.naturalHeight
    const rc = read.getContext('2d')!
    rc.drawImage(img, 0, 0)
    const { data, width, height: h } = rc.getImageData(0, 0, read.width, read.height)
    const N = width * h

    // ── 1. Split the drawing into its three parts ──────────────────────────────────────────────

    type Kind = 'ring' | 'wire' | 'key'
    /** One source pixel with the paper knocked out, or null where it was only paper. */
    const pixel = (x: number, y: number): { alpha: number; kind: Kind } | null => {
      const i = (y * width + x) * 4
      const [r, g, b, a] = [data[i], data[i + 1], data[i + 2], data[i + 3]]

      // White to alpha: keep the colour, drop the paper it was printed on.
      const min = Math.min(r, g, b)
      const alpha = a * (1 - min / 255)
      if (alpha < 0.5) return null
      const scale = 255 / (255 - min)
      const [R, G, B] = [r, g, b].map(c => Math.min(255, Math.max(0, 255 - (255 - c) * scale)))

      const sat = Math.max(R, G, B) - Math.min(R, G, B)
      const ring = sat >= 30 && B >= G && B - R > 20
      const wire = sat >= 30 && !ring && G >= B
      return { alpha: alpha / 255, kind: ring ? 'ring' : wire ? 'wire' : 'key' }
    }

    const masks: Record<Kind, Float32Array> = {
      ring: new Float32Array(N),
      wire: new Float32Array(N),
      key: new Float32Array(N),
    }
    // The ink's box is the mark; the ring's box locates the handle.
    let x0 = width
    let y0 = h
    let x1 = -1
    let y1 = -1
    let rx0 = width
    let ry0 = h
    let rx1 = -1
    let ry1 = -1
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < width; x++) {
        const i = y * width + x
        const white = data[i * 4] > 235 && data[i * 4 + 1] > 235 && data[i * 4 + 2] > 235
        const p = pixel(x, y)
        if (p) masks[p.kind][i] = p.alpha
        if (data[i * 4 + 3] > 40 && !white) {
          if (x < x0) x0 = x
          if (x > x1) x1 = x
          if (y < y0) y0 = y
          if (y > y1) y1 = y
          if (p?.kind === 'ring') {
            if (x < rx0) rx0 = x
            if (x > rx1) rx1 = x
            if (y < ry0) ry0 = y
            if (y > ry1) ry1 = y
          }
        }
      }
    }

    // The ring's centre, and its inner edge: walk out from the centre until the ring starts, in
    // every direction the key doesn't block, and take the median.
    const cx = (rx0 + rx1) / 2
    const cy = (ry0 + ry1) / 2
    const outer = Math.max(rx1 - rx0, ry1 - ry0) / 2
    const hits: number[] = []
    for (let k = 0; k < 180; k++) {
      const t = (k / 180) * Math.PI * 2
      for (let r = outer * 0.5; r < outer; r++) {
        const i = Math.round(cy + r * Math.sin(t)) * width + Math.round(cx + r * Math.cos(t))
        if (masks.key[i] > 0.5) break
        if (masks.ring[i] > 0.5) {
          hits.push(r)
          break
        }
      }
    }
    hits.sort((a, b) => a - b)
    const inner = hits[Math.floor(hits.length / 2)] + 1

    // ── 2. Surfaces ────────────────────────────────────────────────────────────────────────────

    /** Three box blurs make a gaussian; a blurred outline is a rounded bevel. */
    const blur = (src: Float32Array, radius: number) => {
      let a = src
      const run = (from: Float32Array, horizontal: boolean) => {
        const out = new Float32Array(N)
        const len = horizontal ? width : h
        const lines = horizontal ? h : width
        const at = (line: number, k: number) => (horizontal ? line * width + k : k * width + line)
        const w = radius * 2 + 1
        for (let line = 0; line < lines; line++) {
          let sum = 0
          for (let k = -radius; k <= radius; k++) sum += from[at(line, Math.min(len - 1, Math.max(0, k)))]
          for (let k = 0; k < len; k++) {
            out[at(line, k)] = sum / w
            sum += from[at(line, Math.min(len - 1, k + radius + 1))] - from[at(line, Math.max(0, k - radius))]
          }
        }
        return out
      }
      for (let pass = 0; pass < 3; pass++) a = run(run(a, true), false)
      return a
    }

    const norm = (v: number[]) => {
      const m = Math.hypot(...v)
      return v.map(c => c / m)
    }
    const LIGHT = norm([-0.55, -0.75, 0.9])
    const HALF = norm([LIGHT[0], LIGHT[1], LIGHT[2] + 1])
    const clamp01 = (v: number) => Math.min(1, Math.max(0, v))
    const smooth = (e0: number, e1: number, v: number) => {
      const t = clamp01((v - e0) / (e1 - e0))
      return t * t * (3 - 2 * t)
    }

    /** Diffuse light, the sky it reflects, and a specular glint, for a height map at pixel i. */
    const lit = (height: Float32Array, i: number, bevel: number, shine: number) => {
      const x = i % width
      const y = (i - x) / width
      const l = height[x > 0 ? i - 1 : i]
      const r = height[x < width - 1 ? i + 1 : i]
      const u = height[y > 0 ? i - width : i]
      const d = height[y < h - 1 ? i + width : i]
      const [nx, ny, nz] = norm([(l - r) * bevel, (u - d) * bevel, 1])
      const diffuse = Math.max(0, nx * LIGHT[0] + ny * LIGHT[1] + nz * LIGHT[2])
      const specular = Math.max(0, nx * HALF[0] + ny * HALF[1] + nz * HALF[2]) ** shine
      // Reflected view ray: pointing up the image (ry < 0) sees the bright sky, down sees the floor.
      const ry = 2 * nz * ny
      const sky = smooth(0.35, -0.45, ry)
      return { diffuse, specular, sky }
    }

    type Rgb = [number, number, number]
    const hex = (s: string): Rgb => [1, 3, 5].map(k => parseInt(s.slice(k, k + 2), 16) / 255) as Rgb
    /** A gradient map: brightness 0–1 to a colour. */
    const ramp = (stops: [number, string][]) => {
      const parsed = stops.map(([t, c]) => [t, hex(c)] as const)
      return (t: number): Rgb => {
        const v = clamp01(t)
        for (let k = 1; k < parsed.length; k++) {
          if (v <= parsed[k][0]) {
            const [t0, c0] = parsed[k - 1]
            const [t1, c1] = parsed[k]
            const f = (v - t0) / (t1 - t0 || 1)
            return [0, 1, 2].map(j => c0[j] + (c1[j] - c0[j]) * f) as Rgb
          }
        }
        return parsed[parsed.length - 1][1] as Rgb
      }
    }

    // Deterministic value noise, for the metal's grain.
    const hash = (n: number) => {
      const s = Math.sin(n * 127.1) * 43758.5453
      return s - Math.floor(s)
    }
    const noise1 = (v: number) => {
      const k = Math.floor(v)
      const f = v - k
      return hash(k) + (hash(k + 1) - hash(k)) * f * f * (3 - 2 * f)
    }
    const grain = (v: number) => noise1(v * 0.45) * 0.5 + noise1(v * 1.7 + 31) * 0.3 + noise1(v * 5.3 + 77) * 0.2

    const ringHeight = blur(masks.ring, 7)
    const keyHeight = blur(masks.key, 5)
    const wireHeight = blur(masks.wire, 2)
    const wireGlow = blur(masks.wire, 9)
    // The key's shadow falls down and to the right of it.
    const keyShadowSoft = blur(masks.key, 7)
    const SHADOW_DX = 9
    const SHADOW_DY = 12

    const RING = ramp([
      [0, '#020b1c'],
      [0.32, '#07346f'],
      [0.62, '#1464cf'],
      [0.84, '#4fa2ff'],
      [1, '#d6ebff'],
    ])
    const KEY = ramp([
      [0, '#07090d'],
      [0.28, '#232c37'],
      [0.52, '#5c6979'],
      [0.74, '#aab6c3'],
      [0.9, '#e9eef4'],
      [1, '#ffffff'],
    ])
    const WIRE = ramp([
      [0, '#013520'],
      [0.4, '#049a58'],
      [0.72, '#19e08a'],
      [0.9, '#7dffc4'],
      [1, '#f0fff7'],
    ])
    const BOARD_CENTRE = hex('#0d3346')
    const BOARD_EDGE = hex('#03101c')
    const GLOW = hex('#16f08e')

    // ── 3. Paint every layer, bottom up, into premultiplied colour ─────────────────────────────

    /** `keyLift` brightens the steel: on a dark page, gunmetal would sink into the background. */
    const paint = (withKey: boolean, keyLift = 0) => {
      const out = new Float32Array(N * 4)
      const over = (i: number, rgb: Rgb, alpha: number) => {
        const o = i * 4
        const keep = 1 - alpha
        out[o] = rgb[0] * alpha + out[o] * keep
        out[o + 1] = rgb[1] * alpha + out[o + 1] * keep
        out[o + 2] = rgb[2] * alpha + out[o + 2] * keep
        out[o + 3] = alpha + out[o + 3] * keep
      }
      const darken = (i: number, amount: number) => {
        const o = i * 4
        out[o] *= 1 - amount
        out[o + 1] *= 1 - amount
        out[o + 2] *= 1 - amount
      }

      for (let i = 0; i < N; i++) {
        const x = i % width
        const y = (i - x) / width
        const dx = x - cx
        const dy = y - cy
        const dist = Math.hypot(dx, dy)

        // The board: dark, lit from the centre, falling into shadow under the bezel's lip, with a
        // faint sheen across its upper left like glass over it. Traces glow onto it.
        const board = clamp01(inner + 1.5 - dist)
        if (board > 0) {
          const t = (dist / inner) ** 1.4
          const rgb = [0, 1, 2].map(j => BOARD_CENTRE[j] + (BOARD_EDGE[j] - BOARD_CENTRE[j]) * t) as Rgb
          const lip = smooth(inner - 22, inner, dist)
          const sheen = smooth(inner * 0.75, 0, Math.hypot(dx + inner * 0.3, dy + inner * 0.42)) * 0.07
          const glow = wireGlow[i] * 0.55
          over(
            i,
            rgb.map((c, j) => c * (1 - 0.55 * lip) + sheen + GLOW[j] * glow) as Rgb,
            board,
          )
        }

        if (masks.wire[i] > 0) {
          const { diffuse, specular, sky } = lit(wireHeight, i, 5, 24)
          const tone = 0.22 + 0.5 * diffuse + 0.3 * sky
          const rgb = WIRE(tone).map(c => c + specular * 0.5) as Rgb
          over(i, rgb, masks.wire[i])
        }

        if (masks.ring[i] > 0) {
          const { diffuse, specular, sky } = lit(ringHeight, i, 7, 60)
          // Turned on a lathe: fine concentric rings, and the upper half catching more light.
          const turned = (grain(dist * 1.3) - 0.5) * 0.09
          const fall = (dy / outer) * 0.1
          const tone = 0.12 + 0.42 * diffuse + 0.46 * sky + turned - fall
          const rgb = RING(tone).map(c => c + specular * 0.85) as Rgb
          over(i, rgb, masks.ring[i])
        }

        if (withKey) {
          const sx = x - SHADOW_DX
          const sy = y - SHADOW_DY
          if (sx >= 0 && sy >= 0) darken(i, keyShadowSoft[sy * width + sx] * 0.6 * (1 - masks.key[i]))

          if (masks.key[i] > 0) {
            const { diffuse, specular, sky } = lit(keyHeight, i, 5, 90)
            // Brushed along the shaft, which runs from lower left to upper right.
            const brushed = (grain((x + y) * 1.3) - 0.5) * 0.05
            const tone = 0.02 + keyLift + 0.36 * diffuse + 0.4 * sky + brushed
            const rgb = KEY(tone).map(c => c + specular * 1.1) as Rgb
            over(i, rgb, masks.key[i])
          }
        }
      }

      return out
    }

    /** A region of a premultiplied buffer as a canvas. */
    const canvasOf = (buffer: Float32Array, bx: number, by: number, bw: number, bh: number) => {
      const pixels = new ImageData(bw, bh)
      for (let y = 0; y < bh; y++) {
        for (let x = 0; x < bw; x++) {
          const sx = x + bx
          const sy = y + by
          if (sx < 0 || sy < 0 || sx >= width || sy >= h) continue
          const o = (sy * width + sx) * 4
          const a = buffer[o + 3]
          if (a <= 0) continue
          pixels.data.set(
            [buffer[o] / a, buffer[o + 1] / a, buffer[o + 2] / a, a].map(c => Math.round(clamp01(c) * 255)),
            (y * bw + x) * 4,
          )
        }
      }
      const c = document.createElement('canvas')
      c.width = bw
      c.height = bh
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

    const full = paint(true)
    const fullOnDark = paint(true, 0.2)
    const handleOnly = paint(false)

    const bw = x1 - x0 + 1
    const bh = y1 - y0 + 1
    const markCanvas = canvasOf(full, x0, y0, bw, bh)
    const markDarkCanvas = canvasOf(fullOnDark, x0, y0, bw, bh)
    const markWidth = Math.round((bw / bh) * height)

    const side = Math.max(rx1 - rx0, ry1 - ry0) + 5
    const handle = canvasOf(handleOnly, Math.round((rx0 + rx1 - side) / 2), Math.round((ry0 + ry1 - side) / 2), side, side)

    /** The handle on a navy square lit from the upper left, for the home-screen and install icons. */
    const icon = (size: number) => {
      const c = document.createElement('canvas')
      c.width = size
      c.height = size
      const cc = c.getContext('2d')!
      const g = cc.createRadialGradient(size * 0.35, size * 0.3, 0, size * 0.5, size * 0.5, size * 0.75)
      const [r, gg, b] = navy
      g.addColorStop(0, `rgb(${r + 22},${gg + 40},${b + 62})`)
      g.addColorStop(1, `rgb(${Math.round(r * 0.5)},${Math.round(gg * 0.5)},${Math.round(b * 0.55)})`)
      cc.fillStyle = g
      cc.fillRect(0, 0, size, size)
      const d = Math.round(size * iconFill)
      const at = Math.round((size - d) / 2)
      // A soft shadow under the handle, so the bezel stands off the tile.
      cc.shadowColor = 'rgb(0 0 0 / 55%)'
      cc.shadowBlur = size * 0.05
      cc.shadowOffsetY = size * 0.02
      cc.drawImage(scaled(handle, d, d), at, at)
      return base64(c)
    }


    return {
      markLight: base64(scaled(markCanvas, markWidth, height)),
      markDark: base64(scaled(markDarkCanvas, markWidth, height)),
      markWidth,
      icon512: icon(512),
      icon192: icon(192),
      appleTouch: icon(180),
      favicon32: base64(scaled(handle, 32, 32)),
      favicon: base64(scaled(handle, faviconSize, faviconSize)),
      preview: previewHeight
        ? {
            mark: base64(scaled(markCanvas, Math.round((bw / bh) * previewHeight), previewHeight)),
            markDark: base64(scaled(markDarkCanvas, Math.round((bw / bh) * previewHeight), previewHeight)),
            icon: icon(1024),
          }
        : null,
    }
  },
  [source, HEIGHT, NAVY, ICON_FILL, FAVICON_SVG_SIZE, PREVIEW_DIR ? PREVIEW_HEIGHT : 0] as [
    string,
    number,
    number[],
    number,
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

// The handle's dark board and lit bezel read on light and dark tab strips alike, so one copy.
const S = FAVICON_SVG_SIZE
await writeFile(
  publicDir('favicon.svg'),
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${S} ${S}">
<image width="${S}" height="${S}" href="data:image/png;base64,${images.favicon}"/>
</svg>
`,
)

if (PREVIEW_DIR && images.preview) {
  await writeFile(`${PREVIEW_DIR}/mark-large.png`, png64(images.preview.mark))
  await writeFile(`${PREVIEW_DIR}/mark-dark-large.png`, png64(images.preview.markDark))
  await writeFile(`${PREVIEW_DIR}/icon-large.png`, png64(images.preview.icon))
}

console.log(
  `Wrote mark-light.png and mark-dark.png at ${images.markWidth}×${HEIGHT}, plus favicon.svg, ` +
    `favicon-32.png, apple-touch-icon.png, icon-192.png and icon-512.png. ` +
    `Logo.tsx's MARK_RATIO must match ${images.markWidth} / ${HEIGHT}.`,
)
