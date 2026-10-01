import { useState } from 'react'
import { Outlet } from 'react-router'
import { Wallpaper } from '../components/brand/Wallpaper'
import { RouteProgress } from '../components/RouteProgress'
import { SkipLink } from '../components/SkipLink'
import { MobileTabBar } from './MobileTabBar'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { useLayoutMode } from './useLayoutMode'

/**
 * The signed-in frame: sidebar (desktop), icon rail (tablet) or bottom tabs (phone), over the
 * wallpaper, which every page shows so the glass always has something to bend.
 */
export function AppShell() {
  const mode = useLayoutMode()
  const [railExpanded, setRailExpanded] = useState(false)

  return (
    <div className="mf-shell" data-mode={mode}>
      <Wallpaper place="app" />
      <SkipLink />
      <RouteProgress />
      {mode !== 'mobile' && (
        <Sidebar
          expanded={mode === 'desktop' || railExpanded}
          collapsible={mode === 'tablet'}
          onToggle={setRailExpanded}
        />
      )}
      <div className="mf-shell-main">
        <TopBar mode={mode} />
        <main className="mf-content" id="main-content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
      {mode === 'mobile' && <MobileTabBar />}
    </div>
  )
}
