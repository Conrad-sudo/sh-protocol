import { Outlet, redirect, type RouteObject } from 'react-router'
import { RedirectIfSignedIn } from './auth/RedirectIfSignedIn'
import { RequireAuth } from './auth/RequireAuth'
import { FullPageLoader } from './components/FullPageLoader'
import { AuthLayout } from './layouts/AuthLayout'
import { PublicLayout } from './layouts/PublicLayout'
import { HomePage } from './pages/HomePage'
import { NotFoundPage } from './pages/NotFoundPage'
import { RouteError } from './pages/RouteError'

/**
 * The whole route tree. Exported bare so tests can mount it in a memory router.
 *
 * The pages a visitor lands on — home and legal — are in the first bundle. Sign-in (which needs the
 * browser wallet) and the signed-in app are loaded on demand (`lazy`), which keeps wagmi, viem and
 * the chat Markdown renderer out of that first load; the guards stay eager so a signed-out visitor
 * is sent to /login without downloading the app.
 */
export const routes: RouteObject[] = [
  {
    errorElement: <RouteError />,
    HydrateFallback: FullPageLoader,
    children: [
      {
        element: <PublicLayout />,
        children: [
          { index: true, element: <HomePage /> },
          { path: 'terms', lazy: async () => ({ Component: (await import('./pages/legal/TermsPage')).TermsPage }) },
          {
            path: 'privacy',
            lazy: async () => ({ Component: (await import('./pages/legal/PrivacyPage')).PrivacyPage }),
          },
        ],
      },
      {
        element: (
          <RedirectIfSignedIn>
            <AuthLayout />
          </RedirectIfSignedIn>
        ),
        children: [
          { path: 'login', lazy: async () => ({ Component: (await import('./pages/LoginPage')).LoginPage }) },
          // Sign-up became sign-in (the first one creates the account); old links still arrive, `?next=` and all.
          // Nothing to render (the loader redirects first); `null` says so, where none would warn.
          { path: 'signup', loader: ({ request }) => redirect(`/login${new URL(request.url).search}`), element: null },
        ],
      },
      {
        element: (
          <RequireAuth>
            <Outlet />
          </RequireAuth>
        ),
        children: [
          {
            lazy: async () => ({ Component: (await import('./layouts/AppRoute')).AppRoute }),
            children: [
              {
                path: 'dashboard',
                lazy: async () => ({ Component: (await import('./pages/DashboardPage')).DashboardPage }),
              },
              {
                path: 'assistant',
                lazy: async () => ({ Component: (await import('./pages/AssistantPage')).AssistantPage }),
              },
              {
                path: 'history',
                lazy: async () => ({ Component: (await import('./pages/HistoryPage')).HistoryPage }),
              },
              {
                path: 'contacts',
                lazy: async () => ({ Component: (await import('./pages/ContactsPage')).ContactsPage }),
              },
              {
                path: 'controls',
                lazy: async () => ({ Component: (await import('./pages/ControlsPage')).ControlsPage }),
              },
              {
                path: 'settings',
                lazy: async () => ({ Component: (await import('./pages/SettingsPage')).SettingsPage }),
              },
              {
                path: 'onboarding',
                lazy: async () => ({ Component: (await import('./pages/onboarding/OnboardingPage')).OnboardingPage }),
              },
              {
                path: 'wallets/new',
                lazy: async () => ({ Component: (await import('./pages/onboarding/OnboardingPage')).OnboardingPage }),
              },
            ],
          },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
