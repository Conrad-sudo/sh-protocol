import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Archivo with its width axis: the same family is the text at 100% and the headings at 125%.
import '@fontsource-variable/archivo/wdth.css'
import '@fontsource-variable/jetbrains-mono'
// Order matters: tokens.css overrides RSuite's variables using the same selectors.
import 'rsuite/dist/rsuite.min.css'
import './styles/tokens.css'
import './index.css'
import './styles/app.css'
import { ThemeProvider } from './theme/ThemeProvider'
import App from './App.tsx'
import { router } from './router'

const container = document.getElementById('root')!
const root = createRoot(container)

function render() {
  // Also lets the prerendered landing demo, held still until now, play (app.css).
  container.removeAttribute('data-prerendered')
  root.render(
    <StrictMode>
      <ThemeProvider>
        <App />
      </ThemeProvider>
    </StrictMode>,
  )
}

// A prerendered public page (scripts/prerender) is replaced, not hydrated. It stays on screen until
// the router has loaded the page's code: rendering sooner would swap it for the loading screen.
if (container.hasChildNodes() && !router.state.initialized) {
  const stop = router.subscribe(state => {
    if (state.initialized) {
      stop()
      render()
    }
  })
} else {
  render()
}
