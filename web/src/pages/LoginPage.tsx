import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import type { Address } from 'viem'
import { createSiweMessage } from 'viem/siwe'
import { useConnection, useDisconnect, useSignMessage } from 'wagmi'
import { Button, Loader, Message, Text } from 'rsuite'
import { siweAccount, siweLogin, siweNonce } from '../api/auth'
import { useAuth } from '../auth/useAuth'
import { AddressText } from '../components/AddressText'
import { PageMeta } from '../components/PageMeta'
import { ConnectDialog } from '../components/wallet/ConnectDialog'
import { errorText, isUserRejection } from '../lib/tx'
import { WalletProvider } from '../wallet/WalletProvider'

/** The message's purpose, as the wallet shows it. */
const STATEMENT = 'Sign in to Mitfah with this wallet.'

/**
 * Signing in, which is done with the browser wallet alone: connect it, then sign a short message.
 * Signing is free and moves nothing. The first sign-in for an address creates its account, so the
 * page calls it signing up for an address the API doesn't know yet. On
 * success the session starts, and RedirectIfSignedIn (around this page) sends the user on to
 * `?next=` or the dashboard.
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
  const [connectOpen, setConnectOpen] = useState(false)
  // A returning address signs in; a new one signs up. Asked again whenever the wallet switches account.
  const account = useQuery({
    queryKey: ['siwe-account', address],
    queryFn: () => siweAccount(address!),
    enabled: !!address,
  })

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
    onSuccess: signIn,
  })

  const connected = isConnected && !!address
  // If the check fails, the address is treated as new: the same signature signs in either way.
  const returning = account.data?.registered === true
  // "Sign in" until the check says the connected address is new.
  const heading = (
    <h1 className="mf-auth-title">
      {connected && !account.isPending && !returning ? 'Sign up with your wallet' : 'Sign in with your wallet'}
    </h1>
  )

  if (!connected) {
    return (
      <>
        {heading}
        <Text muted>
          Use the browser wallet that owns, or will own, your Mitfah smart wallet — such as MetaMask. First time? Signing
          in creates your account.
        </Text>
        <Button appearance="primary" block size="lg" className="mf-step-action" onClick={() => setConnectOpen(true)}>
          Connect wallet
        </Button>
        <ConnectDialog open={connectOpen} onClose={() => setConnectOpen(false)} />
      </>
    )
  }

  if (account.isPending) {
    return (
      <>
        {heading}
        <Loader className="mf-step-action" content="Checking this address…" />
      </>
    )
  }

  return (
    <div className="mf-verify">
      {heading}
      {returning ? (
        <>
          <Text weight="semibold">Welcome back.</Text>
          <Text>
            Sign a message with <AddressText address={address} />.
          </Text>
        </>
      ) : (
        <>
          <Text>
            Sign a message with <AddressText address={address} /> to prove it's yours. Signing is free and doesn't move
            any funds.
          </Text>
          <Message type="info" showIcon className="mf-settings-note">
            <strong>This address is your Mitfah account.</strong> It's how you sign in, and the only address that can
            pause your smart wallet, change its limits or withdraw.
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
        loading={submit.isPending}
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
