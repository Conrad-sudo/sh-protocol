import { CONTACT_EMAIL, LegalPage } from './LegalPage'

/** DRAFT privacy policy. Every item below matches what the app actually stores (app/db.py). */
export function PrivacyPage() {
  return (
    <LegalPage title="Privacy Policy" updated="17 September 2026">
      <p>
        This policy explains what Mitfah stores about you, why, and who else sees it. Mitfah is an AI assistant that
        operates a smart contract wallet you own, within limits you set.
      </p>

      <h2>What we store</h2>
      <ul>
        <li>
          <strong>Account:</strong> your email address and a hash of your password (never the password itself). If you
          sign in with Google, the account ID Google gives us, not your Google password.
        </li>
        <li>
          <strong>Wallet owner:</strong> the address of the browser wallet you linked as the owner, and the address of
          each Mitfah wallet you created, per network.
        </li>
        <li>
          <strong>Assistant keys:</strong> the key the assistant uses to act for each wallet. It is stored encrypted, and
          the encryption key is kept in a separate key service, not in our database.
        </li>
        <li>
          <strong>Contacts:</strong> the names and addresses you save as people the assistant may pay.
        </li>
        <li>
          <strong>Chat history:</strong> your messages to the assistant and its replies, including the tools it used and
          their results, kept per wallet so the conversation can continue.
        </li>
        <li>
          <strong>Telegram:</strong> if you link Telegram, the numeric ID of that chat.
        </li>
        <li>
          <strong>Sign-in sessions:</strong> a fingerprint (hash) of each session token, so we can sign you out and
          detect a stolen token. A session lasts up to 30 days.
        </li>
        <li>
          <strong>Server logs:</strong> technical records such as IP addresses and request times, used to run and
          protect the service.
        </li>
      </ul>
      <p>We never have the key to your owner wallet, and we don't sell your data.</p>

      <h2>In your browser</h2>
      <p>
        We set one cookie, which keeps you signed in. It can't be read by page scripts and is only sent to our sign-in
        service. Your browser also remembers your theme, the network you last chose, and any transaction still waiting
        for confirmation. We use no advertising or analytics cookies.
      </p>

      <h2>Who else sees your data</h2>
      <ul>
        <li>
          <strong>Anthropic</strong> runs the AI model. Your chat messages, and the wallet details the assistant needs to
          answer, are sent to it to produce each reply.
        </li>
        <li>
          <strong>Blockchain networks.</strong> Every transaction your wallet makes is public and permanent. Anyone can
          see your wallet's address, balances and payments. We send transactions and read balances through
          blockchain node providers.
        </li>
        <li>
          <strong>Google</strong>, only if you sign in with Google.
        </li>
        <li>
          <strong>Telegram</strong>, only if you link it. Messages you send the bot pass through Telegram.
        </li>
        <li>
          <strong>WalletConnect</strong>, only if you connect a wallet with it.
        </li>
        <li>Hosting and infrastructure providers that run the service for us.</li>
        <li>Authorities, when the law requires it.</li>
      </ul>

      <h2>How long we keep it</h2>
      <p>
        We keep your account data while your account exists. Expired sign-in sessions and one-time codes are useless
        after they expire. Blockchain records can't be deleted by anyone.
      </p>

      <h2>Your choices</h2>
      <ul>
        <li>You can remove contacts and unlink Telegram at any time in the app.</li>
        <li>
          To get a copy of your data, correct it, or delete your account, write to {CONTACT_EMAIL}. Deleting your account
          doesn't delete your Mitfah wallet or its funds: they stay yours, and you can still withdraw them with your
          owner wallet.
        </li>
        <li>Depending on where you live, you may have further rights, such as complaining to a data protection authority.</li>
      </ul>

      <h2>Changes</h2>
      <p>If this policy changes in a way that matters, we'll tell you in the app or by email before it takes effect.</p>

      <h2>Contact</h2>
      <p>Questions about privacy: {CONTACT_EMAIL}.</p>
    </LegalPage>
  )
}
