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
| `npm run wallpaper` | Redraws the wallpaper (landing, sign-in, dashboard) to `public/brand/wallpaper-*.svg` |
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
whole path in one sitting (sign up → create → fund → contact → reload → pause → withdraw →
unpause); the others each go deep on one part.

Set `VITE_API_URL` only if the API is served from a different origin (e.g. `https://api.mitfah.com`);
by default requests go to the same origin.

**Google sign-in** uses the same `GOOGLE_CLIENT_ID` as the API: `vite.config.ts` reads it from the
repo's `.env` (only that one value — the rest of that file never reaches the bundle).
`VITE_GOOGLE_CLIENT_ID` in `web/.env.local` overrides it. With neither set, Google buttons are
hidden. Setup steps are in [docs/setup.md](../docs/setup.md).

## Going live

| Setting | Where | What it is |
|---|---|---|
| `CORS_ORIGINS` | API | Every origin the site is served from, comma-separated. The refresh cookie needs credentialed CORS, so a missing origin signs people out. |
| `SIWE_DOMAIN` | API | The site a wallet-binding message must name (`mitfah.com`). The check is what stops a phishing page binding someone else's address. |
| `COOKIE_SECURE` | API | `1` in production; `0` only for local http. |
| `JWT_SECRET` | API | A long random string. |
| `GOOGLE_CLIENT_ID` | API + build | Google sign-in. Without it the Google buttons stay hidden. |
| `TELEGRAM_BOT_USERNAME` | API | Needed to mint Telegram deep links. |
| `VITE_WALLETCONNECT_PROJECT_ID` | build | A Reown project id. Empty means WalletConnect is dropped from the bundle entirely; with one, it is loaded on demand. |

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

- `src/styles/tokens.css` — brand colours and fonts, layered over RSuite's CSS variables. Any token
  written as `var(--mf-…)` must also be re-declared in the `.rs-theme-dark` block. Colour carries
  meaning: navy (light) or steel (dark) is the owner's own action, green only ever means the wallet
  is live (active, assistant on, what's left to spend), amber loosens a limit, red is a brake. The
  type is one family, Archivo: stretched to 125% (`--mf-wide`) for headings and figures, normal
  width for text; JetBrains Mono is only for addresses and hashes. For status labels use
  `StatusTag`, not RSuite's coloured `Tag` (its white-on-colour text fails contrast). Use the
  `.mf-num` class for numbers that should line up.
- `src/components/LimitDial.tsx` — the spending limit as a gauge (a `progressbar`), echoing the
  wallpaper's ring. It is the one bold element wherever it appears: the landing page's demo
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
  sign-in and sign-up card and the dashboard: the logo's ring as a polished metal band (with its
  crescent, and a gap where the key's shaft would cross), and circuit traces running from it to
  every edge. Light mode has raised steel traces; dark mode has glowing emerald ones. The 3D and the
  glow are drawn, not filtered, so the files stay sharp and cheap to paint. A fixed seed lays out
  the traces, so a re-run writes the same files; change `SEED` for a different layout.
  `components/brand/Wallpaper.tsx` puts it on a page as a fixed layer, so the page scrolls over it;
  its `place` picks where the ring sits, and `app.css` (the Wallpaper section) positions it by the
  ring, which is the centre of the 2880px square. Over it, cards turn to frosted glass and text on
  the wallpaper gets a halo in the page colour; it's hidden for anyone asking for more contrast.
