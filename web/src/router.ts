import { createBrowserRouter } from 'react-router'
import { routes } from './routes'

/** The app's one router. main.tsx waits on it before replacing a prerendered page. */
export const router = createBrowserRouter(routes)
