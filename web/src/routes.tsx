import type { RouteObject } from 'react-router'
import { RedirectIfSignedIn } from './auth/RedirectIfSignedIn'
import { RequireAuth } from './auth/RequireAuth'
import { AppShell } from './layouts/AppShell'
import { AuthLayout } from './layouts/AuthLayout'
import { PublicLayout } from './layouts/PublicLayout'
import { AssistantPage } from './pages/AssistantPage'
import { ContactsPage } from './pages/ContactsPage'
import { ControlsPage } from './pages/ControlsPage'
import { DashboardPage } from './pages/DashboardPage'
import { HomePage } from './pages/HomePage'
import { LoginPage } from './pages/LoginPage'
import { NotFoundPage } from './pages/NotFoundPage'
import { OnboardingPage } from './pages/onboarding/OnboardingPage'
import { RouteError } from './pages/RouteError'
import { SettingsPage } from './pages/SettingsPage'
import { SignupPage } from './pages/SignupPage'

/** The whole route tree. Exported bare so tests can mount it in a memory router. */
export const routes: RouteObject[] = [
  {
    errorElement: <RouteError />,
    children: [
      {
        element: <PublicLayout />,
        children: [{ index: true, element: <HomePage /> }],
      },
      {
        element: (
          <RedirectIfSignedIn>
            <AuthLayout />
          </RedirectIfSignedIn>
        ),
        children: [
          { path: 'login', element: <LoginPage /> },
          { path: 'signup', element: <SignupPage /> },
        ],
      },
      {
        element: (
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        ),
        children: [
          { path: 'dashboard', element: <DashboardPage /> },
          { path: 'assistant', element: <AssistantPage /> },
          { path: 'contacts', element: <ContactsPage /> },
          { path: 'controls', element: <ControlsPage /> },
          { path: 'settings', element: <SettingsPage /> },
          { path: 'onboarding', element: <OnboardingPage /> },
          { path: 'wallets/new', element: <OnboardingPage /> },
        ],
      },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
