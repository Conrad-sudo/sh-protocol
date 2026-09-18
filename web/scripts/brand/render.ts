// Turns Key-logo.png into the two marks in public/brand/. Run with `npm run marks`.
//
// The artwork is drawn on white paper, so the first job is to knock the paper out to alpha —
// otherwise the ring would sit on a white disc on a navy sidebar. The second is the dark copy:
// the same shape with the navy ring and green traces swapped for their on-dark values and the
// black key inverted to white. Both run in a browser canvas, so no image library is needed.
import { chromium } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

/** Mark height in the output files. Nothing shows the mark above 64px, so this is plenty. */
const HEIGHT = 384
const RING_ON_DARK = [127, 176, 230] // #7FB0E6, the same pale blue as favicon.svg's dark ring
const WIRE_ON_DARK = [43, 183, 126] // #2BB77E, the brand green on dark

// Handed to the page as a data URL: a file:// image on about:blank cannot be decoded, and one
// loaded cross-origin would taint the canvas and block getImageData.
const png = await readFile(fileURLToPath(new URL('./Key-logo.png', import.meta.url)))
const source = `data:image/png;base64,${png.toString('base64')}`
const out = (name: string) => fileURLToPath(new URL(`../../public/brand/${name}`, import.meta.url))

const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto('about:blank')

const marks = await page.evaluate(
  async ([src, height, ringOnDark, wireOnDark]: [string, number, number[], number[]]) => {
    const img = new Image()
    img.src = src
    await img.decode()

    const read = document.createElement('canvas')
    read.width = img.naturalWidth
    read.height = img.naturalHeight
    const rc = read.getContext('2d')!
    rc.drawImage(img, 0, 0)
    const { data, width, height: h } = rc.getImageData(0, 0, read.width, read.height)

    // The ink is everything opaque that is not near-white: that box is the mark.
    let x0 = width
    let y0 = h
    let x1 = -1
    let y1 = -1
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < width; x++) {
        const i = (y * width + x) * 4
        const white = data[i] > 235 && data[i + 1] > 235 && data[i + 2] > 235
        if (data[i + 3] > 40 && !white) {
          if (x < x0) x0 = x
          if (x > x1) x1 = x
          if (y < y0) y0 = y
          if (y > y1) y1 = y
        }
      }
    }
    const bw = x1 - x0 + 1
    const bh = y1 - y0 + 1

    const light = new ImageData(bw, bh)
    const dark = new ImageData(bw, bh)
    for (let y = 0; y < bh; y++) {
      for (let x = 0; x < bw; x++) {
        const i = ((y + y0) * width + (x + x0)) * 4
        const o = (y * bw + x) * 4
        const [r, g, b, a] = [data[i], data[i + 1], data[i + 2], data[i + 3]]

        // White to alpha: keep the colour, drop the paper it was printed on.
        const min = Math.min(r, g, b)
        const alpha = a * (1 - min / 255)
        if (alpha < 0.5) continue
        const scale = 255 / (255 - min)
        const rgb = [r, g, b].map(c => Math.min(255, Math.max(0, 255 - (255 - c) * scale)))

        light.data.set([...rgb, alpha], o)

        // The anti-aliasing now lives in the alpha, so each colour can be assigned flat.
        const [R, G, B] = rgb
        const sat = Math.max(R, G, B) - Math.min(R, G, B)
        const ring = sat >= 30 && B >= G && B - R > 20
        const wire = sat >= 30 && !ring && G >= B
        dark.data.set(
          [...(ring ? ringOnDark : wire ? wireOnDark : rgb.map(c => 255 - c)), alpha],
          o,
        )
      }
    }

    const width_ = Math.round((bw / bh) * height)
    const encode = (pixels: ImageData) => {
      const full = document.createElement('canvas')
      full.width = bw
      full.height = bh
      full.getContext('2d')!.putImageData(pixels, 0, 0)

      const small = document.createElement('canvas')
      small.width = width_
      small.height = height
      const sc = small.getContext('2d')!
      sc.imageSmoothingQuality = 'high'
      sc.drawImage(full, 0, 0, width_, height)
      return small.toDataURL('image/png').split(',')[1]
    }
    return { light: encode(light), dark: encode(dark), width: width_ }
  },
  [source, HEIGHT, RING_ON_DARK, WIRE_ON_DARK] as [string, number, number[], number[]],
)

await browser.close()
await writeFile(out('mark-light.png'), Buffer.from(marks.light, 'base64'))
await writeFile(out('mark-dark.png'), Buffer.from(marks.dark, 'base64'))
console.log(
  `Wrote mark-light.png and mark-dark.png at ${marks.width}×${HEIGHT}. ` +
    `Logo.tsx's MARK_RATIO must match ${marks.width} / ${HEIGHT}.`,
)
