import type { ReactNode } from 'react'
import KeyIcon from '@rsuite/icons/Key'
import PauseRoundIcon from '@rsuite/icons/PauseRound'
import PeoplesIcon from '@rsuite/icons/Peoples'
import ShieldIcon from '@rsuite/icons/Shield'
import { Link } from 'react-router'
import { Text } from 'rsuite'
import { useAuth } from '../auth/useAuth'
import { Wallpaper } from '../components/brand/Wallpaper'
import { LinkButton } from '../components/LinkButton'
import { StatusTag } from '../components/StatusTag'
import { useChains } from '../hooks/useChains'
import { chainName } from '../wallet/chains'

const STEPS = [
  {
    title: 'Create your wallet',
    body: 'Sign up, connect the browser wallet you already use, and create a Mitfah wallet. Your browser wallet owns it.',
  },
  {
    title: 'Set your limit',
    body: 'Choose how many dollars the assistant may spend each day and add funds. Save the people it may pay as contacts.',
  },
  {
    title: 'Just ask',
    body: 'Tell the assistant what to do, here or in Telegram: “Send Sam 20 USDC” or “What did I spend today?”',
  },
]

const SAFETY: { icon: ReactNode; title: string; body: string }[] = [
  {
    icon: <KeyIcon />,
    title: 'You own it',
    body: 'Your browser wallet is the owner. Only you can change the rules or withdraw everything.',
  },
  {
    icon: <ShieldIcon />,
    title: 'A daily dollar limit',
    body: 'The wallet itself refuses any payment that would go over your limit, whatever the assistant is told.',
  },
  {
    icon: <PauseRoundIcon />,
    title: 'Pause any time',
    body: 'One click stops every payment. You can still withdraw your money while the wallet is paused.',
  },
  {
    icon: <PeoplesIcon />,
    title: 'Pays only your contacts',
    body: 'The assistant can send money only to people you saved as contacts, and it can’t add new ones.',
  },
]

const FAQ = [
  {
    q: 'Does Mitfah hold my money?',
    a: 'No. Your money sits in a smart contract wallet that your own browser wallet owns. Mitfah never has your owner key, so it can’t take the funds or change your limits.',
  },
  {
    q: 'What can the assistant do?',
    a: 'Check balances, send tokens to your contacts, swap tokens and answer questions about your wallet — always within your daily limit.',
  },
  {
    q: 'What happens if the assistant makes a mistake?',
    a: 'It can never spend more than your daily limit or pay anyone who isn’t a saved contact. If something looks wrong, pause the wallet and withdraw your funds.',
  },
  {
    q: 'What does it cost?',
    a: 'Each action the assistant takes pays the network fee plus a small protocol fee of a few cents, both from the wallet’s own balance. Neither counts toward your spending limit, and you can cap the network fee in Controls.',
  },
  {
    q: 'Which wallets can I connect?',
    a: 'Any browser wallet, such as MetaMask, Rabby or Coinbase Wallet. You need one to own your Mitfah wallet.',
  },
  {
    q: 'Can I use it from Telegram?',
    a: 'Yes. After you sign up, link Telegram in Settings and chat with the same assistant from your phone. It follows the same limits.',
  },
]

export function HomePage() {
  const { status } = useAuth()
  const signedIn = status === 'signedIn'

  const cta = signedIn ? (
    <LinkButton to="/dashboard" appearance="primary" size="lg">
      Open your dashboard
    </LinkButton>
  ) : (
    <LinkButton to="/signup" appearance="primary" size="lg">
      Get started
    </LinkButton>
  )

  return (
    <>
      <title>Mitfah — an AI assistant for your crypto wallet</title>
      <Wallpaper place="hero" />

      <section className="mf-hero">
        <h1>An AI assistant for your crypto wallet that can only spend what you allow.</h1>
        <Text muted size="lg">
          You own the wallet. You set a daily dollar limit. You can pause it any time.
        </Text>
        <div className="mf-hero-actions">
          {cta}
          {!signedIn && (
            <LinkButton to="/login" size="lg">
              Sign in
            </LinkButton>
          )}
        </div>
      </section>

      <section className="mf-landing-section" aria-labelledby="how-heading">
        <h2 id="how-heading">How it works</h2>
        <ol className="mf-steps-list">
          {STEPS.map((step, i) => (
            <li key={step.title} className="mf-landing-card">
              <span className="mf-step-number" aria-hidden>
                {i + 1}
              </span>
              <h3>{step.title}</h3>
              <Text muted>{step.body}</Text>
            </li>
          ))}
        </ol>
      </section>

      <section className="mf-landing-section" aria-labelledby="safety-heading">
        <h2 id="safety-heading">Built to keep your money safe</h2>
        <ul className="mf-safety-list">
          {SAFETY.map(item => (
            <li key={item.title} className="mf-landing-card">
              <span className="mf-safety-icon" aria-hidden>
                {item.icon}
              </span>
              <h3>{item.title}</h3>
              <Text muted>{item.body}</Text>
            </li>
          ))}
        </ul>
      </section>

      <Networks signedIn={signedIn} />

      <section className="mf-landing-section mf-telegram-band" aria-labelledby="telegram-heading">
        <h2 id="telegram-heading">Also in Telegram</h2>
        <Text muted>
          Link your account in Settings and ask the assistant from any chat. Same wallet, same limits, same history.
        </Text>
      </section>

      <section className="mf-landing-section mf-faq" aria-labelledby="faq-heading">
        <h2 id="faq-heading">Questions</h2>
        {FAQ.map(item => (
          <details key={item.q} className="mf-faq-item" name="faq">
            <summary>{item.q}</summary>
            <Text muted>{item.a}</Text>
          </details>
        ))}
      </section>

      <section className="mf-landing-section mf-final-cta">
        <h2>Ready when you are</h2>
        <div className="mf-hero-actions">{cta}</div>
      </section>
    </>
  )
}

/** The networks this server serves. Nothing is shown until it knows, and nothing if it can't say. */
function Networks({ signedIn }: { signedIn: boolean }) {
  const { data: chains } = useChains()
  if (!chains?.length) return null

  return (
    <section className="mf-landing-section" aria-labelledby="networks-heading">
      <h2 id="networks-heading">Networks</h2>
      <ul className="mf-network-list">
        {chains.map(chain => (
          <li key={chain.chain_id}>
            {chainName(chain.chain_id)}
            {chain.fork && <StatusTag tone="neutral">Test network</StatusTag>}
          </li>
        ))}
      </ul>
      <Text muted>
        You get a separate wallet on each network. <Link to={signedIn ? '/wallets/new' : '/signup'}>Add one</Link> whenever you like.
      </Text>
    </section>
  )
}
