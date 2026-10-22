import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { readChoice } from '../theme/themeStore'
import { ConfirmPage } from './ConfirmPage'
import { tokenFromPath } from './token'
import './confirm.css'

// The theme chosen in the app, if any; otherwise the stylesheet follows the system.
const choice = readChoice()
if (choice !== 'system') document.documentElement.dataset.theme = choice

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ConfirmPage token={tokenFromPath(window.location.pathname)} />
  </StrictMode>,
)
