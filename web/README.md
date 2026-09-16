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
| `npm run lint` | Oxlint |
| `npm run typecheck` | TypeScript only |

## Where things live

- `src/styles/tokens.css` — brand colours and fonts, layered over RSuite's CSS variables. Any token
  written as `var(--mf-…)` must also be re-declared in the `.rs-theme-dark` block.
- `src/theme/` — light/dark handling. `index.html` repeats the same rule in a small script so dark
  mode applies before the first paint.
- `public/brand/`, `public/favicon.svg`, `public/icon-*.png` — logo assets derived from
  `../Key-logo.png`.
