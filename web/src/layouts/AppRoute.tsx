import { WalletProvider } from '../wallet/WalletProvider'
import { AppShell } from './AppShell'

/** The signed-in app: its frame, with browser-wallet support around it. Loaded on demand. */
export function AppRoute() {
  return (
    <WalletProvider>
      <AppShell />
    </WalletProvider>
  )
}
