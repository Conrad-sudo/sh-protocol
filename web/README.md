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
| `npm run build` | Type-check and build to `dist/` |
| `npm run preview` | Serve the built `dist/` |
| `npm test` | Unit tests (Vitest) |
| `npm run e2e` | Browser tests (Playwright) at desktop, tablet and phone sizes in both themes; the API is mocked, so no back end is needed. First run: `npx playwright install chromium` |
| `npm run og` | Re-renders the social share image to `public/og.png` |
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

- `src/styles/tokens.css` — brand colours and fonts, layered over RSuite's CSS variables. Any token
  written as `var(--mf-…)` must also be re-declared in the `.rs-theme-dark` block. For status labels
  use `StatusTag`, not RSuite's coloured `Tag` (its white-on-colour text fails contrast). Use the
  `.mf-num` class for numbers that should line up (tabular figures are off by default, because
  Inter's version also widens hyphens).
- `src/theme/` — light/dark handling. `index.html` repeats the same rule in a small script so dark
  mode applies before the first paint.
- `public/brand/`, `public/favicon.svg`, `public/icon-*.png` — logo assets derived from
  `../Key-logo.png`.
