import type { ReactNode } from 'react'
import KeyIcon from '@rsuite/icons/Key'
import PauseRoundIcon from '@rsuite/icons/PauseRound'
import PeoplesIcon from '@rsuite/icons/Peoples'
import ShieldIcon from '@rsuite/icons/Shield'
import TimeIcon from '@rsuite/icons/Time'
import DetailIcon from '@rsuite/icons/Detail'
import { Link } from 'react-router'
import { useAuth } from '../auth/useAuth'
import { Wallpaper } from '../components/brand/Wallpaper'
import { GlassLayer } from '../components/Glass'
import { LimitDemo } from '../components/landing/LimitDemo'
import { LinkButton } from '../components/LinkButton'
import { PageMeta, SITE_URL } from '../components/PageMeta'
import { StatusTag } from '../components/StatusTag'
import { useChains } from '../hooks/useChains'
import { chainName } from '../wallet/chains'

const STEPS = [
  {
    title: 'Create your wallet',
    body: 'Sign in with the browser wallet you already use — it is your account — and create a Mitfah smart wallet. Your browser wallet owns it.',
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

/** What the assistant might try, and what the wallet does about it. */
const RULES: { icon: ReactNode; tries: string; wallet: string }[] = [
  {
    icon: <ShieldIcon />,
    tries: 'Spend more than your daily limit',
    wallet: 'Refuses the payment. The limit is part of the wallet itself, whatever the assistant is told.',
  },
  {
    icon: <PeoplesIcon />,
    tries: 'Pay someone you haven’t saved',
    wallet: 'Won’t send it. The assistant pays only your contacts, and it can’t add new ones.',
  },
  {
    icon: <DetailIcon />,
    tries: 'Send money without asking',
    wallet: 'Waits for you. Every payment is quoted first, with what it costs, and goes only when you say yes.',
  },
  {
    icon: <KeyIcon />,
    tries: 'Change your limit or rules',
    wallet: 'Can’t. Only your browser wallet can, because it owns the Mitfah smart wallet.',
  },
  {
    icon: <TimeIcon />,
    tries: 'Keep its access for good',
    wallet: 'Loses it after 30 days unless you renew it. You can turn it off sooner.',
  },
  {
    icon: <PauseRoundIcon />,
    tries: 'Keep going when you want it to stop',
    wallet: 'Stops everything in one click when you pause it. You can still withdraw while it’s paused.',
  },
]

const FAQ = [
  {
    q: 'What is Mitfah?',
    a: 'A smart wallet with an AI assistant. Tell it what you want in plain words, like “swap 50 USDC for ETH” or “send 20 USDC to Sam”, and it does it on-chain for you, always within the limits you set. It’s built on the SessionHandler protocol.',
  },
  {
    q: 'What is a smart wallet?',
    a: 'A wallet that is a smart contract (a small program on the blockchain) instead of a single private key. Because it runs code, it can enforce rules on every payment, such as a daily spending limit, who it may pay, and a pause button. Your browser wallet owns it, so only you can change those rules.',
  },
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
    a: 'Any browser wallet, such as MetaMask, Rabby or Coinbase Wallet. You sign in with it, and it owns your Mitfah smart wallet. On a phone, open Mitfah in your wallet app’s browser.',
  },
  {
    q: 'Can I use it from Telegram?',
    a: 'Yes. After you sign in, link Telegram in Settings and chat with the same assistant from your phone. It follows the same limits.',
  },
]

const DESCRIPTION =
  'Send and swap crypto by chat. Your wallet enforces the rules, your AI assistant follows them. You own the smart contract wallet, set a daily dollar limit and can pause it any time.'

/** What the site is and who publishes it, for search engines (schema.org JSON-LD). */
const STRUCTURED_DATA = JSON.stringify({
  '@context': 'https://schema.org',
  '@graph': [
    {
      '@type': 'Organization',
      '@id': `${SITE_URL}/#organization`,
      name: 'Mitfah',
      url: `${SITE_URL}/`,
      logo: `${SITE_URL}/icon-512.png`,
    },
    {
      '@type': 'WebSite',
      '@id': `${SITE_URL}/#website`,
      name: 'Mitfah',
      url: `${SITE_URL}/`,
      publisher: { '@id': `${SITE_URL}/#organization` },
    },
    {
      '@type': 'WebApplication',
      name: 'Mitfah',
      url: `${SITE_URL}/`,
      description: DESCRIPTION,
      applicationCategory: 'FinanceApplication',
      operatingSystem: 'Any',
      browserRequirements: 'A browser wallet such as MetaMask, Rabby or Coinbase Wallet',
      publisher: { '@id': `${SITE_URL}/#organization` },
    },
  ],
})

export function HomePage() {
  const { status } = useAuth()
  const signedIn = status === 'signedIn'

  const cta = signedIn ? (
    <LinkButton to="/dashboard" appearance="primary" size="lg">
      Open your dashboard
    </LinkButton>
  ) : (
    <LinkButton to="/login" appearance="primary" size="lg">
      Get started
    </LinkButton>
  )

  return (
    <>
      <PageMeta title="Mitfah — an AI assistant for your crypto wallet" description={DESCRIPTION} path="/" />
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: STRUCTURED_DATA }} />
      <Wallpaper place="hero" />

      <section className="mf-hero">
        <h1>Send and swap crypto by chat. Your wallet enforces the rules, your AI assistant follows them.</h1>
        <p className="mf-hero-lede">
          You own the wallet and set a daily dollar limit. The wallet itself refuses anything over it, whatever the
          assistant is told.
        </p>
        <div className="mf-hero-actions">{cta}</div>
      </section>

      <LimitDemo />

      <section className="mf-landing-section" aria-labelledby="how-heading">
        <h2 id="how-heading">How it works</h2>
        <ol className="mf-trace">
          {STEPS.map((step, i) => (
            <li key={step.title}>
              <span className="mf-trace-pad" aria-hidden>
                {i + 1}
              </span>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="mf-landing-section" aria-labelledby="safety-heading">
        <h2 id="safety-heading">Built to keep your money safe</h2>
        <p className="mf-section-lede">
          These rules are written into your wallet on the blockchain. Neither the assistant nor Mitfah can switch them
          off.
        </p>
        {/* The glass sits beside the table, not in it: a table may only hold its rows. */}
        <div className="mf-rules-plate mf-glass-surface">
          <GlassLayer />
          <table className="mf-rules">
            <thead>
              <tr>
                <th scope="col">If the assistant tries to</th>
                <th scope="col">Your wallet</th>
              </tr>
            </thead>
            <tbody>
              {RULES.map(rule => (
                <tr key={rule.tries}>
                  <th scope="row">
                    <span className="mf-rule-icon" aria-hidden>
                      {rule.icon}
                    </span>
                    {rule.tries}
                  </th>
                  <td>{rule.wallet}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <Networks signedIn={signedIn} />

      <section className="mf-landing-section mf-telegram-band" aria-labelledby="telegram-heading">
        <h2 id="telegram-heading">Also in Telegram</h2>
        <p>Link your account in Settings and ask the assistant from any chat. Same wallet, same limits, same history.</p>
      </section>

      <section className="mf-landing-section mf-faq" aria-labelledby="faq-heading">
        <h2 id="faq-heading">FAQs</h2>
        {FAQ.map(item => (
          <details key={item.q} className="mf-faq-item" name="faq">
            <summary>{item.q}</summary>
            <p>{item.a}</p>
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
      <p className="mf-section-note">
        You get a separate wallet on each network. <Link to={signedIn ? '/wallets/new' : '/login'}>Add one</Link>{' '}
        whenever you like.
      </p>
    </section>
  )
}
