/*
 * Where the confirm page is open, and how it leaves. Telegram's own browser can't use passkeys on
 * either phone, so the page hands itself to the phone's own browser, which can (Face ID, a
 * fingerprint, the screen lock), and closes Telegram's browser behind it. Once the payment is done
 * there, it heads back to Telegram.
 *
 * Read in Telegram-iOS (submodules/BrowserUI/Sources/BrowserWebContent.swift, 2026-10-26):
 * - Its browser is a web view inside the app, and iOS lets an app's web view use passkeys only for
 *   that app's own domains, so the prompt is refused at once.
 * - It puts TelegramWebviewProxy on every page before any script runs. That is the sign to go by:
 *   its user agent copies Safari's.
 * - A page's move to any scheme but http(s), tonsite and about is cancelled and handed to iOS
 *   (openExternalUrl with forceExternal): no tap needed, no question asked. iOS opens
 *   x-safari-https://<rest> in Safari as https://<rest>.
 * - window.close() closes the browser (webViewDidClose). WebKit lets a page do that when it is the
 *   only page the view has shown, as a link Telegram opened is.
 *
 * Read in Telegram for Android (DrKLO/Telegram: ui/web/BotWebViewContainer.java,
 * messenger/browser/Browser.java, 2026-10-26):
 * - Its browser is an Android WebView that never turns on WebAuthn (no setWebAuthenticationSupport),
 *   so passkeys are refused there too.
 * - It adds TelegramWebviewProxy to the page as well (addJavascriptInterface). Older versions named
 *   it TelegramWebview.
 * - shouldOverrideUrlLoading hands an intent:// address to Android (openInExternalApp), again with
 *   no tap. intent://<host><path>#Intent;scheme=https;end names no app, so Android opens the
 *   https address in the phone's default browser: Telegram claims only t.me and telegram.me.
 * - window.close() closes the browser (onCloseWindow), on the same terms as WebKit's.
 * - Chrome won't open an app from a timer (developer.chrome.com/docs/android/intents), so on Android
 *   the way back is Back, or the page's Back to Telegram link.
 */

export type Phone = 'ios' | 'android'

/** The phone's system, or null on a computer. */
export function phone(): Phone | null {
  const ua = navigator.userAgent
  // iPadOS Safari says it is a Mac; only the touch screen gives it away.
  if (/iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) return 'ios'
  return /Android/.test(ua) ? 'android' : null
}

/** Whether this page is open in Telegram's own browser on a phone, and which. */
export function telegramBrowser(): Phone | null {
  const telegram = 'TelegramWebviewProxy' in window || 'TelegramWebviewProxyProto' in window || 'TelegramWebview' in window
  return telegram ? phone() : null
}

/** The address that opens `href` (http or https) in the phone's own browser: Safari, or Android's default. */
export function browserUrl(href: string, on: Phone): string {
  if (on === 'ios') return `x-safari-${href}`
  const url = new URL(href)
  return `intent://${url.host}${url.pathname}${url.search}#Intent;scheme=${url.protocol.slice(0, -1)};end`
}

/** Opens the chat with the bot in the Telegram app, from a browser beside it. */
export function telegramAppUrl(bot: string): string {
  return `tg://resolve?domain=${bot}`
}

/** Leaving the page, in one place so tests can watch it: jsdom can't navigate or close a window. */
export const browserNav = {
  go(url: string) {
    window.location.href = url
  },
  close() {
    window.close()
  },
}

/**
 * Calls `onLeft` once the page is put away (its app sent to the background, or another app in
 * front), and `onStayed` if that hasn't happened within `ms`.
 *
 * @return  Stops both.
 */
export function whenLeft(onLeft: () => void, onStayed?: () => void, ms = 3_000): () => void {
  let left = false
  const leave = () => {
    if (left || document.visibilityState !== 'hidden') return
    left = true
    onLeft()
  }
  document.addEventListener('visibilitychange', leave)
  const timer = onStayed
    ? setTimeout(() => {
        if (!left) onStayed()
      }, ms)
    : undefined
  return () => {
    document.removeEventListener('visibilitychange', leave)
    clearTimeout(timer)
  }
}
