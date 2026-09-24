import { Link } from 'react-router'
import { CONTACT_EMAIL, LegalPage } from './LegalPage'

/** DRAFT terms of service. */
export function TermsPage() {
  return (
    <LegalPage
      title="Terms of Service"
      description="The terms for using Mitfah: your account, your wallet and its limits, the AI assistant, fees, risks and liability."
      path="/terms"
      updated="17 September 2026"
    >
      <p>
        These terms cover your use of Mitfah (the website, the app and the Telegram bot). By creating an account you
        agree to them. If you don't agree, please don't use Mitfah.
      </p>

      <h2>What Mitfah is</h2>
      <p>
        Mitfah lets you create a smart contract wallet that you own, and lets an AI assistant make payments and swaps
        from it within the limits you set. Mitfah is software, not a bank, broker or custodian. We don't hold your funds
        and we can't move them outside those limits.
      </p>

      <h2>Your account</h2>
      <ul>
        <li>You must be old enough to agree to these terms where you live, and allowed to use crypto services there.</li>
        <li>Keep your password and your owner wallet safe. Anything done with them is treated as done by you.</li>
        <li>Give us accurate information, and tell us if you think someone else is using your account.</li>
      </ul>

      <h2>Your wallet and your limits</h2>
      <ul>
        <li>
          Your browser wallet owns your Mitfah wallet. If you lose access to it, we can't recover your Mitfah wallet or
          its funds for you.
        </li>
        <li>
          You choose the spending limit, the tokens it counts, the contacts the assistant may pay and the other
          settings. You're responsible for choosing them sensibly.
        </li>
        <li>You can pause the wallet and withdraw your funds at any time with your owner wallet.</li>
      </ul>

      <h2>The AI assistant</h2>
      <ul>
        <li>
          The assistant acts on your instructions, but AI can misunderstand or make mistakes. Check what it tells you,
          and set limits you're comfortable losing.
        </li>
        <li>Nothing the assistant says is financial, legal or tax advice.</li>
        <li>
          Blockchain transactions can't be reversed. Once a payment is made, neither you nor we can undo it.
        </li>
      </ul>

      <h2>Fees</h2>
      <p>
        Each action the assistant takes pays the network fee and a small protocol fee from your Mitfah wallet. The
        protocol fee is a fixed amount of the network's own currency, so its dollar value moves with that
        currency's price, and it may change.
      </p>

      <h2>Acceptable use</h2>
      <p>
        Don't use Mitfah for anything illegal, including fraud, money laundering or evading sanctions, and don't try to
        break, overload or get around the service's protections.
      </p>

      <h2>Risks</h2>
      <p>
        Crypto assets can lose value quickly. Smart contracts, blockchain networks, price feeds and other services
        Mitfah relies on can fail or be attacked. You use Mitfah at your own risk.
      </p>

      <h2>No warranty and limits on liability</h2>
      <p>
        Mitfah is provided “as is”. As far as the law allows, we aren't liable for lost funds, lost profits or indirect
        losses from using it, including losses caused by the assistant acting within the limits you set.
      </p>

      <h2>Ending your use</h2>
      <p>
        You can stop using Mitfah at any time. We may suspend or close accounts that break these terms. Your Mitfah
        wallet stays yours either way, and you can still withdraw its funds with your owner wallet.
      </p>

      <h2>Changes</h2>
      <p>If we change these terms in a way that matters, we'll tell you before the change takes effect.</p>

      <h2>Contact</h2>
      <p>
        Questions: {CONTACT_EMAIL}. See also our <Link to="/privacy">Privacy Policy</Link>.
      </p>
    </LegalPage>
  )
}
