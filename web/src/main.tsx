import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/inter'
import '@fontsource-variable/manrope'
import '@fontsource-variable/jetbrains-mono'
// Order matters: tokens.css overrides RSuite's variables using the same selectors.
import 'rsuite/dist/rsuite.min.css'
import './styles/tokens.css'
import './index.css'
import './styles/app.css'
import { ThemeProvider } from './theme/ThemeProvider'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
)
