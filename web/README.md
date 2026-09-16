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
| `npm run lint` | Oxlint |
| `npm run typecheck` | TypeScript only |

Set `VITE_API_URL` only if the API is served from a different origin (e.g. `https://api.mitfah.com`);
by default requests go to the same origin.

**Google sign-in** uses the same `GOOGLE_CLIENT_ID` as the API: `vite.config.ts` reads it from the
repo's `.env` (only that one value — the rest of that file never reaches the bundle).
`VITE_GOOGLE_CLIENT_ID` in `web/.env.local` overrides it. With neither set, Google buttons are
hidden. Setup steps are in [docs/setup.md](../docs/setup.md).

## Where things live

- `src/api/client.ts` — every API call goes through `apiFetch`. The access token is kept in memory
  only; on a 401 the client refreshes once and retries. **Refreshes are single-flight and
  cross-tab locked**: the server treats a refresh token used twice as stolen and signs the account
  out everywhere.
- `src/auth/` — `AuthProvider` (restores the session on load, syncs sign-out across tabs),
  `RequireAuth` / `RedirectIfSignedIn`, and `safeNext` (the open-redirect guard for `?next=`).
- `src/layouts/` — `AppShell` picks sidebar (≥ 1024px), icon rail (≥ 768px) or bottom tabs;
  `nav.ts` is the one list of sections.
- `src/routes.tsx` — the route tree.

- `src/styles/tokens.css` — brand colours and fonts, layered over RSuite's CSS variables. Any token
  written as `var(--mf-…)` must also be re-declared in the `.rs-theme-dark` block. For status labels
  use `StatusTag`, not RSuite's coloured `Tag` (its white-on-colour text fails contrast). Use the
  `.mf-num` class for numbers that should line up (tabular figures are off by default, because
  Inter's version also widens hyphens).
- `src/theme/` — light/dark handling. `index.html` repeats the same rule in a small script so dark
  mode applies before the first paint.
- `public/brand/`, `public/favicon.svg`, `public/icon-*.png` — logo assets derived from
  `../Key-logo.png`.
