import { type ReactNode, useState } from 'react'
import { useOutletContext } from 'react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Address } from 'viem'
import { createSiweMessage } from 'viem/siwe'
import { useConnection, useDisconnect, useSignMessage } from 'wagmi'
import { Button, Loader, Message, Text } from 'rsuite'
import { passkeyLogin, passkeySignInOptions, siweAccount, siweLogin, siweNonce } from '../api/auth'
import type { PasskeyAssertionJSON, TokenResponse } from '../api/types'
import { useAuth } from '../auth/useAuth'
import { AddressText } from '../components/AddressText'
import { Explained } from '../components/Explained'
import { PageMeta } from '../components/PageMeta'
import { ConnectDialog } from '../components/wallet/ConnectDialog'
import { isPasskeyError, PasskeyError, passkeysSupported, signInWithPasskey } from '../lib/passkey'
import { errorText, isUserRejection } from '../lib/tx'
import type { AuthOutlet } from '../layouts/AuthLayout'
import { WalletProvider } from '../wallet/WalletProvider'

/** The message's purpose, as the wallet shows it. */
const STATEMENT = 'Sign in to Mitfah with this wallet.'

/**
 * Signing in with the browser wallet: connect it, then sign a short message. Signing is free and
 * moves nothing. A returning user whose wallet approves a passkey can sign in with that instead
 * (PasskeySignIn), which is the easy way on a phone. The first sign-in for an address creates its account, so the
 * page calls it signing up for an address the API doesn't know yet. Once the API accepts the
 * signature, the safe dial behind the card turns through its combination and opens (AuthLayout);
 * then the session starts, and RedirectIfSignedIn (around this page) sends the user on to `?next=`
 * or the dashboard.
 *
 * Loaded on demand like the signed-in app, so wagmi stays out of the landing page's first load.
 */
export function LoginPage() {
  return (
    <WalletProvider>
      <PageMeta
        title="Sign in · Mitfah"
        description="Sign in to Mitfah with your wallet to manage your Mitfah smart wallet, change its limits and talk to your assistant."
        path="/login"
      />
      <SignIn />
    </WalletProvider>
  )
}

function SignIn() {
  const { address, chainId, isConnected } = useConnection()
  const { mutate: disconnect } = useDisconnect()
  const { mutateAsync: signMessageAsync } = useSignMessage()
  const { signIn } = useAuth()
  const { openSafe } = useOutletContext<AuthOutlet>()
  const [connectOpen, setConnectOpen] = useState(false)
  // Whether the address was a returning one, held from the signature on: starting the session
  // empties the query cache, the address check below with it, and the page should stay as it was
  // while it gives way to the next one.
  const [held, setHeld] = useState<boolean>()
  // A returning address signs in; a new one signs up. Asked again whenever the wallet switches account.
  const account = useQuery({
    queryKey: ['siwe-account', address],
    queryFn: () => siweAccount(address!),
    enabled: !!address,
  })

  const connected = isConnected && !!address
  const checking = account.isPending && held === undefined
  // If the check fails, the address is treated as new: the same signature signs in either way.
  const returning = held ?? account.data?.registered === true

  /** Starts the session once the API has said yes, whichever way the user signed in. */
  const enter = async (token: TokenResponse) => {
    // The signed-in app loads while the dial turns, so it's ready when the safe opens.
    import('../layouts/AppRoute').catch(() => {})
    import('./DashboardPage').catch(() => {})
    await openSafe()
    signIn(token)
  }

  const submit = useMutation({
    mutationFn: async (account: Address) => {
      const { nonce } = await siweNonce()
      const issuedAt = new Date()
      // The domain is this page's own host: the API refuses a message written for any other site.
      const message = createSiweMessage({
        domain: window.location.host,
        address: account,
        statement: STATEMENT,
        uri: window.location.origin,
        version: '1',
        chainId: chainId ?? 1,
        nonce,
        issuedAt,
        expirationTime: new Date(issuedAt.getTime() + 10 * 60_000),
      })
      const signature = await signMessageAsync({ message, account })
      return siweLogin({ message, signature, nonce })
    },
    onSuccess: async token => {
      setHeld(returning)
      await enter(token)
    },
  })

  // "Sign in" until the check says the connected address is new; what to do next beside it.
  const heading = (explanation?: ReactNode) => (
    <Explained
      level={1}
      title={connected && !checking && !returning ? 'Sign up with your wallet' : 'Sign in with your wallet'}
      titleClassName="mf-auth-title"
    >
      {explanation}
    </Explained>
  )

  if (!connected) {
    return (
      <>
        {heading(
          'Use the browser wallet that owns, or will own, your Mitfah wallet, such as MetaMask. First time? Signing in creates your account.',
        )}
        <Button appearance="primary" block size="lg" className="mf-step-action" onClick={() => setConnectOpen(true)}>
          Connect wallet
        </Button>
        <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
        {passkeysSupported() && <PasskeySignIn onToken={enter} />}
      </>
    )
  }

  if (checking) {
    return (
      <>
        {heading()}
        <Loader className="mf-step-action" content="Checking this address…" />
      </>
    )
  }

  return (
    <div className="mf-verify">
      {returning ? (
        heading(
          <>
            Welcome back. Sign a message with <AddressText address={address} />.
          </>,
        )
      ) : (
        <>
          {heading(
            <>
              Sign a message with <AddressText address={address} /> to prove it's yours. It's free and moves no funds.
            </>,
          )}
          <Message type="info" showIcon className="mf-settings-note">
            <strong>This address is your Mitfah account.</strong> Only it can sign in, change your wallet or withdraw.
          </Message>
        </>
      )}
      {submit.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          {isUserRejection(submit.error) ? 'Cancelled — nothing was signed.' : errorText(submit.error)}
        </Message>
      )}
      <Button
        appearance="primary"
        block
        size="lg"
        className="mf-step-action"
        // Still busy once signed in: the safe is opening, and the next page is on its way.
        loading={submit.isPending || submit.isSuccess}
        onClick={() => submit.mutate(address)}
      >
        {returning ? 'Sign in' : 'Sign up'}
      </Button>
      <Text className="mf-auth-alt" muted>
        Not this address? Switch accounts in your wallet, or{' '}
        <Button appearance="link" size="sm" onClick={() => disconnect()}>
          connect another wallet
        </Button>
        .
      </Text>
    </div>
  )
}

const SIGNIN_OPTIONS_KEY = ['passkey-signin-options'] as const

/** How long fetched sign-in options are tapped with. The server keeps each challenge five minutes. */
const SIGNIN_OPTIONS_FRESH_MS = 4 * 60_000

/**
 * Signing in with a passkey: for a returning user whose wallet already approves a passkey on this
 * device (or one synced to it). No account is named; the passkey the user picks names it. One that no
 * wallet approves is refused, and so is everyone signing in for the first time: they use the wallet.
 *
 * Safari opens the passkey prompt only inside the tap, so the options are fetched before it, and
 * again while the page waits (each challenge is good once, for five minutes).
 */
function PasskeySignIn({ onToken }: { onToken: (token: TokenResponse) => Promise<void> }) {
  const queryClient = useQueryClient()
  const options = useQuery({
    queryKey: SIGNIN_OPTIONS_KEY,
    queryFn: async () => (await passkeySignInOptions()).options,
    staleTime: SIGNIN_OPTIONS_FRESH_MS,
    refetchInterval: SIGNIN_OPTIONS_FRESH_MS,
    gcTime: 0,
    retry: false,
  })
  const submit = useMutation({
    mutationFn: async (pending: Promise<PasskeyAssertionJSON>) => passkeyLogin(await pending),
    onSuccess: onToken,
    // A challenge is good once, whatever the answer was: the next tap needs a fresh one.
    onError: () => void options.refetch(),
    networkMode: 'always',
  })

  /** Opens the passkey prompt. Call it straight from the tap. */
  const start = () => {
    submit.reset()
    const fresh = options.data && Date.now() - options.dataUpdatedAt < SIGNIN_OPTIONS_FRESH_MS ? options.data : null
    const pending = fresh
      ? signInWithPasskey(fresh)
      : // Not fetched in time: fetched now, after which a browser may refuse to open the prompt
        // outside the tap. The options are then ready for the next one.
        queryClient
          .fetchQuery({ queryKey: SIGNIN_OPTIONS_KEY, queryFn: async () => (await passkeySignInOptions()).options, staleTime: 0 })
          .then(signInWithPasskey)
          .catch((error: unknown) => {
            if (isPasskeyError(error, 'cancelled')) {
              throw new PasskeyError('cancelled', 'Your browser needs one more tap. Tap the button again.')
            }
            throw error
          })
    submit.mutate(pending)
  }

  return (
    <>
      <Text className="mf-auth-alt" muted>
        Or, if your wallet already approves a passkey on this device:
      </Text>
      <Button
        appearance="ghost"
        block
        size="lg"
        className="mf-step-action"
        // Still busy once signed in: the safe is opening, and the next page is on its way.
        loading={submit.isPending || submit.isSuccess}
        onClick={start}
      >
        Sign in with a passkey
      </Button>
      {submit.isError && (
        <Message type="error" showIcon className="mf-settings-note">
          {errorText(submit.error)}
        </Message>
      )}
    </>
  )
}
