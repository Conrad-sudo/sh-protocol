# Mitfah web app

The browser front end for mitfah.com: sign in, deploy a wallet, set its spending limits and chat with
the assistant. It talks to the FastAPI back end in `../app/api.py`.

Stack: Vite, React 19 (with the React Compiler), TypeScript, RSuite 6, TanStack Query, wagmi + viem.
Needs **Node 22.22 or later**.

## Run it

Two terminals, from the repo root:

```bash
COOKIE_SECURE=0 make api        # API on :8000 (add APP_FORK_MODE=1 to use the local fork)
cd web && npm install && npm run dev   # site on http://localhost:3000
```

The dev server always uses port 3000: the API's default `CORS_ORIGINS` and `SIWE_DOMAIN` both name
it, so a different port would break sign-in and wallet linking. Requests to `/api` are proxied to
the API, so the page and the API share an origin.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Dev server with hot reload |
| `npm run build` | Type-check, build to `dist/` and prerender the public pages (needs Playwright's Chromium) |
| `npm run prerender` | Prerender only, after `vite build`: writes `/`, `/terms` and `/privacy` into `dist/` as finished HTML |
| `npm run preview` | Serve the built `dist/` |
| `npm test` | Unit tests (Vitest) |
| `npm run e2e` | Browser tests (Playwright) at desktop, tablet and phone sizes in both themes; the API is mocked, so no back end is needed. First run: `npx playwright install chromium` |
| `npm run og` | Re-renders the social share image to `public/og.png` |
| `npm run marks` | Rebuilds the light and dark logo marks from `scripts/brand/Key-logo.png` |
| `npm run wallpaper` | Redraws the wallpaper's circuit (landing, sign-in, every signed-in page) to `public/brand/wallpaper-*.svg`; the safe dial in its middle is drawn on the page |
| `npm run lint` | Oxlint |
| `npm run typecheck` | TypeScript only |

### Against a real chain

`e2e/real/` signs and sends real transactions on a local fork and writes to `wallet.db`, so it runs
only when asked for:

```bash
E2E_REAL=1 npx playwright test real --project=desktop-light
```

It needs Vault, `make sepolia-fork` and the API with `APP_FORK_MODE=1`. A restarted fork has no
protocol on it — `make deploy ARGS=sepolia-fork && make db` puts it back. `real/journey` walks the
whole path in one sitting (sign in → create → fund → contact → reload → pause → withdraw →
unpause); the others each go deep on one part.

Set `VITE_API_URL` only if the API is served from a different origin (e.g. `https://api.mitfah.com`);
by default requests go to the same origin.

**Signing in** is done with the browser wallet alone (SIWE): `/login` connects it and asks it to
sign a message for this site, and an address's first sign-in creates its account. There is no
email, password or Google sign-in; `/signup` redirects to `/login`. The page is loaded on demand
with wagmi, like the signed-in app. On a phone, signing in needs WalletConnect
(`VITE_WALLETCONNECT_PROJECT_ID`) or the wallet app's own browser.

## Going live

| Setting | Where | What it is |
|---|---|---|
| `CORS_ORIGINS` | API | Every origin the site is served from, comma-separated. The refresh cookie needs credentialed CORS, so a missing origin signs people out. |
| `SIWE_DOMAIN` | API | The site a sign-in message must name (`mitfah.com`). The check is what stops a phishing page replaying someone's signature to sign in as them. |
| `COOKIE_SECURE` | API | `1` in production; `0` only for local http. |
| `JWT_SECRET` | API | A long random string. |
| `TELEGRAM_BOT_USERNAME` | API | Needed to mint Telegram deep links. |
| `VITE_WALLETCONNECT_PROJECT_ID` | build | A Reown project id. Empty means WalletConnect is dropped from the bundle entirely; with one, it is loaded on demand. Without it, a phone can sign in only from a wallet app's own browser. |

**Hosting.** `dist/` is static. Serve `/terms` from `terms.html` and `/privacy` from
`privacy.html` (most static hosts do this for "clean URLs"), and answer every other address that
isn't a file with `index.html`, so links into the app work. Those three pages are prerendered by
`scripts/prerender/render.ts`, so search engines, AI crawlers and link previews read the page
without running JavaScript; the app replaces the copy when it loads. Also set at the host: HTTPS
with HSTS, a redirect from `www.mitfah.com` to `mitfah.com` (the host every canonical link names)
and the usual security headers.

`public/llms.txt` summarises the landing page for AI assistants. Change it when the landing page's
claims change.

Before launch, replace the `[contact email]` placeholder in `src/pages/legal/LegalPage.tsx` and have
a lawyer read `/terms` and `/privacy` — they are drafts, and say so on the page.

## Where things live

- `src/api/client.ts` — every API call goes through `apiFetch`. The access token is kept in memory
  only; on a 401 the client refreshes once and retries. **Refreshes are single-flight and
  cross-tab locked**: the server treats a refresh token used twice as stolen and signs the account
  out everywhere.
- `src/auth/` — `AuthProvider` (restores the session on load, syncs sign-out across tabs),
  `RequireAuth` / `RedirectIfSignedIn`, and `safeNext` (the open-redirect guard for `?next=`).
- `src/layouts/` — `AppShell` picks sidebar (≥ 1024px), icon rail (≥ 768px) or bottom tabs;
  `nav.ts` is the one list of sections.
- `src/routes.tsx` — the route tree. The landing, legal and sign-in pages are in the first bundle;
  everything behind the sign-in is loaded on demand, which is what keeps wagmi, viem and the chat
  Markdown renderer out of a first visit (1.17 MB → 331 kB). Browser wallets are set up in
  `layouts/AppRoute.tsx`, not at the root, for the same reason. A test mounting the tree gets those
  pages loaded for it by `renderRoutes` (`src/test/utils.tsx`), which is why that helper is async.
  RSuite does ship per-component CSS (`rsuite/<Component>/styles/index.css`), but each file repeats
  the same 14 kB of variables, so importing thirty of them is larger than the one stylesheet — it
  was measured and left alone.
- `src/components/QueryError.tsx` — one wording for a failed load, everywhere: "Couldn't load X" and
  a Try again. `SkipLink` and `RouteProgress` sit in every layout: the first link on the page jumps
  past the navigation, and the bar shows while the next page is being fetched.
- `src/lib/assistant.ts` — where the assistant stands (`on`, `expiring`, `expired`, `off`, or
  `foreign` for a key the wallet trusts that Mitfah doesn't hold), read from the wallet's `session`
  block. The Controls row, the dashboard header and the chat notice all go by it. Turning the
  assistant on and renewing it are the same transaction — a brand-new key lasting
  `ASSISTANT_KEY_DAYS` — and `useOwnerAction` finishes it through `/api/wallet/session/confirm`,
  **never** the generic `/api/wallet/tx/confirm`: that one would leave the API signing with the key
  the wallet just dropped.
- **Every wallet wait ends.** Creating a wallet (`useDeploy`), the owner changes (`useOwnerAction`)
  and the Fund drawer (`useFundWallet`) wait the same way, with the timings in `src/lib/tx.ts`.
  Before the wallet answers, "Stop waiting" (or closing the dialog) lets go; a transaction the wallet
  sends anyway is still followed. Once it is sent, the API says whether its node has `seen` the
  hash, and one still unseen after 90 s may never arrive, so the page says so and offers "Check
  again". Creating a wallet is stricter: it offers to start over only for a transaction the network
  never saw, because a second deploy makes a second wallet.

- `src/components/dashboard/AddTokenModal.tsx` — adding a token by its contract address,
  MetaMask-style: paste the address, then check what the chain says it is before adding it. The
  preview confirms it's an ERC-20 token and shows its symbol and decimals (what the server read to
  decide that), its name, the wallet's balance and the whole address, with a copy button. An address
  that can't answer both symbol and decimals gets a warning to check it again; a token the server
  can't show safely — an odd symbol, or one copying a listed token's — is refused with the reason.
  A token Mitfah lists is accepted too: the preview says "Pricing is available" and offers a "Count
  it toward my spending limit" checkbox (ticked, once the owner wallet is connected); adding it then
  sends the owner's `watched-tokens` transaction. If that is cancelled the token stays added,
  uncounted, and the dialog offers to try again. Unpriced tokens show in `BalancesCard` as "No
  price" and "Not limited". Withdraw sends an ERC-20's address, not its ticker, so added tokens
  withdraw like any other.

- **The dashboard shows only the user's tokens**: the native token, the ones picked at deploy (and
  counted since), and the ones added. Every row but the native token and WETH/WBNB has a Remove
  link (`RemoveTokenModal`). A token that still counts goes in two steps — "Stop counting" (the
  owner's signature), then remove — because the dashboard must show everything the limit covers;
  the API refuses the removal otherwise. The page shares one `useOwnerAction` between the header
  actions and `BalancesCard`, so only one owner change is ever in flight.

- **LP tokens.** After the assistant adds liquidity, the pool's LP tokens show as a row like
  "ETH/USDC LP" (`lp: true`), with "≈ 40 ETH + 100,000 USDC" under the amount — what they hold in
  the pool now (`underlying`). The row goes when the wallet holds none and comes back with the next
  deposit, so it has no Remove link, and it isn't flagged "Not limited": the assistant can't send
  it. The Withdraw drawer lists it like any token.

- **WETH/WBNB always count.** `/api/tokens` flags the wrapped native token `always_counted`. The
  onboarding picker shows it ticked and locked ("ETH and WETH always count"); Controls lists it
  under "Always counts" with no Remove, or — on a wallet made before the rule — "Not counted yet"
  with a one-click "Count it". The picker for other tokens never offers it.

- `src/styles/tokens.css` — brand colours and fonts, layered over RSuite's CSS variables. Any token
  written as `var(--mf-…)` must also be re-declared in the `.rs-theme-dark` block. Colour carries
  meaning: navy (light) or steel (dark) is the owner's own action, green only ever means the wallet
  is live (active, assistant on, what's left to spend), amber loosens a limit, red is a brake. The
  type is two faces: Bodoni Moda (`--mf-font-heading`) for the wordmark, h1/h2 titles and the
  dial's figures, held at its size-24 optical cut (set once on `body`) so the hairlines survive a
  1x screen; Archivo for text and small labels, h3 included. JetBrains Mono is only for addresses
  and hashes. For status labels use
  `StatusTag`, not RSuite's coloured `Tag` (its white-on-colour text fails contrast). Use the
  `.mf-num` class for numbers that should line up.
- `src/components/LimitDial.tsx` — the spending limit as a gauge (a `progressbar`), echoing the
  wallpaper's safe dial. It is the one bold element wherever it appears: the landing page's demo
  (`components/landing/LimitDemo.tsx`, a day played once in CSS and shown finished under reduced
  motion), the dashboard and the chat's side panel.
- `src/theme/` — light/dark handling. `index.html` repeats the same rule in a small script so dark
  mode applies before the first paint.
- `scripts/brand/` — `Key-logo.png` is the logo as drawn, flat on white paper; `npm run marks`
  renders it as the objects it shows, lit from the upper left, keeping its exact shapes: the ring as
  an anodized blue bezel, the circuit as vivid green traces on a dark board set inside the ring, the
  key as brushed steel casting a short shadow. Each part's bevel comes from its own outline (the
  mask blurred into a height map, then lit). It writes `public/brand/mark-light.png` (gunmetal key)
  and `mark-dark.png` (the same with brighter steel, which would otherwise sink into a dark page).
  To change the logo, replace `Key-logo.png`, run the script, and set `MARK_RATIO` in
  `src/components/brand/Logo.tsx` to the size it prints; `LOGO_PREVIEW=<dir>` also writes large
  copies there for checking. The same run writes the icons — `public/favicon.svg`,
  `favicon-32.png`, `apple-touch-icon.png` and `icon-*.png` — from the key's handle alone: the lit
  ring and board with the key's shaft left out, so their circuitry is the logo's own. The app icons
  put it on a lit navy tile; the dark board reads on light and dark tab strips alike, so
  `favicon.svg` holds one copy. At 16px the circuit is only a texture inside the ring; that is
  accepted so every icon matches the logo. `npm run og` re-renders the share image, which embeds the
  dark mark.
- `scripts/wallpaper/` — `npm run wallpaper` draws the wallpaper behind the landing page, the
  sign-in card and every page of the signed-in app (`AppShell` renders it, so the glass
  always has something to bend): circuit traces running from a safe dial in the middle out to
  every edge. Light mode has raised steel traces; dark mode has glowing emerald ones. The 3D and the
  glow are drawn, not filtered, so the files stay sharp and cheap to paint. A fixed seed lays out
  the traces, so a re-run writes the same files; change `SEED` for a different layout.
  `components/brand/Wallpaper.tsx` puts it on a page as a fixed layer, so the page scrolls over it;
  its `place` picks where the dial sits, and `app.css` (the Wallpaper section) positions it by the
  dial, which is the centre of the 2880px square. On the sign-in page it shrinks to fit a short or
  narrow window, so the whole dial shows (never below 2300px), and from 600px wide the sign-in card
  is a glass disc filling the dial's face (`--mf-auth-dial` in `app.css`, Auth pages), its text
  centred in a 340px column — sized so the tallest state (a new address's note plus an error) stays
  inside the circle. Below 600px the form lies on the wallpaper with the dial low beneath it. Text
  lying on the wallpaper gets a halo in the page colour; the wallpaper is hidden for anyone asking
  for more contrast.
- `src/components/brand/SafeDial.tsx` — the dial itself, drawn on the page rather than into the
  image so it can turn: a fixed ring (the logo ring's navy metal) with the index at the top, round
  a dial of 100 marks (longer every five and ten, no numbers), with a ridged grip on its inner
  edge. Three stacked SVGs — under the dial, the dial, over it — so only the middle one turns (on
  the GPU, no repaint) while the light and the index stay put. Its colours are `--mf-safe-*` in `app.css`
  ("Safe dial"). Signing in turns it (`openSafe.ts`, called through `AuthLayout`'s outlet
  context): left a full turn to mark 72, then right to 18, where it stays, and the index lights
  green — about 1.5s, while the signed-in app's code loads. Only then does the session start;
  `RedirectIfSignedIn` keeps the page up until the next one is ready and leaves through a view
  transition (`data-safe="open"` on `<html>`): the sign-in page swells and fades with the app behind
  it. Anyone asking for less motion skips the turn and gets the plain cross-fade; jsdom (no
  `Element.animate`) skips it too, so unit tests don't wait. `public/og.png` was drawn from the old
  ring and has not been redrawn.
- `src/components/Glass.tsx` — the bars and cards are liquid glass, drawn by
  [quick-liquid](https://github.com/amarnath3003/quickLiquid): a lens bending the wallpaper through a
  30px rim, a 12px frost, the plate colour at 40% and light on the edge, with 28px corners on plates,
  capsules for the top bar, landing header, phone tab bar and budget strip, and a disc for the
  sign-in card. Only Chromium bends;
  Safari and Firefox get the frost, tint and rim light. The glass is a layer (`GlassLayer`, an
  `aria-hidden` `.mf-glass`) placed as the FIRST child of a surface marked `mf-glass-surface`, behind
  the content rather than wrapped round it: the engine sets `overflow: hidden`, corners and a shadow
  inline on its own element, which on the content would clip the top bar's dropdowns and focus
  rings. Cards use `GlassPanel` (RSuite's `Panel` drawn `as` a glass plate). A list or table can't
  hold the layer, so its plate is a wrapping `div`. The content is positioned so it paints over
  the layer (`app.css`, "Liquid glass"); nothing on a surface or its layer may set `filter`,
  `opacity`, `mask` or `backdrop-filter`, or the lens sees only the surface instead of the
  wallpaper. Until the engine draws, in the prerendered copy (the prerender strips the engine's
  layers, whose SVG filter and lens map don't survive the copy) and under more-contrast or reduced
  transparency, the layer shows a plain frosted plate. jsdom has no `ResizeObserver`, so in Vitest
  the engine stays off and only the markup is tested. On a real GPU (an M1) scrolling holds 60 fps,
  as before; with software rendering (no GPU, as in Playwright's headless browser) scrolling the
  Controls page is about 3x slower than with the old frost, so expect the mocked suite to run
  slower too. The current page's item, in the
  sidebar and the tab bar, sits on a lit pill that slides from the last page (`--mf-nav`/`--mf-tab`).
