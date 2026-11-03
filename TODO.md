# Mitfah: what's left to do

## 1. Bugs

- [ ] `/api/sign/start` gives a bare 500 when the node is down, after hanging about 2 minutes. It should give a clear error.
- [ ] After an approval, an error raised before anything was sent still says "check History", though nothing went
      out and History has no row (`confirm_signed`).
- [ ] "Remove all my liquidity" is refused about half the time (a rounding bug in `langchain-uniswap-v2`). Same bug
      class for "sell all" of 18-decimal tokens. The write-up is in `~/Docs/Foundry/uniswap_v2_toolkit/update.md`. Release
      the fix, bump the 0.4.0 pin, and remove the `math.nextafter` loop in `test_e2e_fork.py`.
- [ ] Live Sepolia has new gas prices (Glamsterdam, about 7x). Check the app's hardcoded or locally simulated gas there,
      and drop `--gas-estimate-multiplier 1000` once Forge handles it.
- [ ] Testing only: the Base fork freezes after about 20 ops (Anvil stuck on a dead Alchemy connection), and forks
      freeze at about 60 approvals at once. Not an app bug, but it limits how big the fork tests can be.

## 2. Contract changes (all ship in one full redeploy, plan and go-ahead first)

- [ ] **Separate the admin from the API's hot key (audit 1.1, launch blocker).** Today the API's first bundler key
      owns the treasury on every live network, so a server breach can fake price feeds and void every cap. Needs
      a separate admin address, hardware wallet or keystore deploys, a multisig + timelock, a delay on `setFeed`,
      `Ownable2Step`, and `renounceOwnership` that reverts.
- [ ] Set the launch fee to $0.10-$0.20 (your call on 2026-11-02). The deploy script still says $0.005 / $0.10 /
      $0.015. Also update the tests, README, docs/contracts.md and THREAT_MODEL §3.12. A higher maximum raises what a
      leaked key can burn per op outside the cap.
- [ ] Publish the real ERC-8004 registration file and replace the made-up `AGENT_URI` (audit 1.5).
- [ ] Multisig on SessionHandler itself, so a wallet can have several owners (your note). Not designed yet. Separate from the admin multisig above.
- [ ] Measure only the tokens a transaction touches, instead of looping over all watched tokens (your idea, up to
      32 tokens, saves gas). Not designed yet. Careful: if the backend supplies the list, a leaked key could leave a
      token out and dodge the cap, so the contract must work out or check the list itself.
- [ ] Owner name in the wallet, and a one-time activation fee with a pay-later option that adds it to the first
      transaction (your notes). Neither exists. Decide if you still want them.
- [ ] Tidy-up (audit 2.4): remove or use `SponsorshipPaymaster.sol` (unused, 0% coverage), SHFactory branch tests,
      `SH_MODULE` / `WALLET_ID` naming, unused `getAgentReputation`, the module's NatSpec pointing to a roadmap file that
      doesn't exist, HelperConfig comments saying EntryPoint v0.9 when the address is v0.7.
- [ ] Libraries (audit 2.3): OpenZeppelin v5.5.0 vs upgradeable v5.6.1, your local edits inside `lib/`, and test
      against EntryPoint v0.7 (the library is v0.9, live chains run v0.7).

## 3. Before launch: security and dependencies

- [ ] External contract audit, once section 2 is frozen (audit 4.1).
- [ ] Bug bounty, live before mainnet holds user funds (audit 4.2).
- [ ] Run the Claude Security scan. Never run so far.
- [ ] Move the bundler keys out of `.env` into a secret manager (audit 1.3).
- [ ] Upgrade vulnerable packages (22 known issues on 2026-10-08: pyjwt, langchain, requests and others; versions
      unchanged since) and lock them with hashes (audit 2.2).
- [ ] Rate limits on the other routes: only about 11 of 52 routes have one, and there's no default (audit 2.5).
- [ ] Bring CI back (audit 2.1). It only runs contract jobs now. Find out why `make py-test` failed on GitHub only,
      then restore Python, web and Dependabot one at a time. Set the RPC secrets: the nightly fork jobs skip themselves and show green. Add `forge fmt --check` after one formatting commit.

## 4. Before launch: running it

- [ ] Docker images or systemd units for the API and the bot, and copying the web build to the server. Caddy is done (audit 3.1).
- [ ] `/healthz` and `/readyz` (audit 3.2).
- [ ] Logs and alerts: JSON logs, bundler balance, stuck nonce, oracle age, sequencer, DeepSeek failures, disk,
      any admin call on chain (audit 3.3).
- [ ] Backups of `wallet.db` and `wallet-chat.db`, with a tested restore (audit 3.4).
- [ ] Runbooks for server or admin key compromise, bundler out of funds, stuck nonce, oracle or sequencer down, RPC or DeepSeek down, DB restore (audit 3.5).
- [ ] Keep Sepolia as staging with production settings (audit 3.6).
- [ ] Bundler keys: automatic top-ups and low-balance alerts, and fill the nonce gap when a private RPC drops a bundle.
      Today that stalls the key until a restart (audit 4.3).
- [ ] Alchemy limits (your note): every chain and fork shares one account, with no second provider to fall back to.
      Pick a paid plan and add failover. History's pacing and retry are already done.

## 5. Before launch: passkey flow

- [ ] Test Telegram to Safari/Chrome and back on a real iPhone and Android phone. I have no record of this passing,
      so tick it if you've done it.
- [ ] Move to the real domain and set `WEB_APP_URL`, `SIWE_DOMAIN`, `CORS_ORIGINS`. Passkeys are tied to the domain for
      life, so ones made on the Tailscale test host won't work on mitfah.com.
- [ ] Run these on the current code (no record of them since the changes): the real web e2e specs, and the agent smoke since the 2026-10-26 chat-memory change.

## 6. Before launch: site and paperwork

- [ ] Replace the `[contact email]` placeholder in `LegalPage.tsx` (audit 1.5).
- [ ] Lawyer review of Terms and Privacy, then remove the Draft banner.
- [ ] Privacy page: it doesn't mention passkeys (public keys, device names) or ENS lookups going to the RPC provider. The wording is your call.
- [ ] Link the public GitHub repo on the site? It proves the claims but shows your handle.
- [ ] Home page title still says "an AI assistant for your crypto wallet", not the new tagline. Intended?
- [ ] `og.png` still shows the old ring, and re-running `npm run og` would lose it. Redraw?
- [ ] Fill the investor brief's placeholders: team, round, traction, use of funds, contact.
- [ ] After going live: Search Console and Bing sitemaps, `/seo google`, Rich Results Test, `curl https://mitfah.com/terms`.

## 7. Product and code clean-up

- [ ] Message in Review & create: if MetaMask says "This transaction is likely to fail", raise the gas limit, with the
      steps (your note). MetaMask overrides the gas we send and a site can't stop it.
- [ ] One place listing every deployed wallet across chains (your note). There's a factory event, a counter and a table
      per chain, but no combined view.
- [ ] A way to read `/feedback` messages. The bot saves them, but nothing reads them.
- [ ] Docs: README:67 "at most $X per day can leave... whatever goes wrong" overclaims (uncounted tokens, NFTs,
      borrowing, the admin key). Contact-only sending is a server rule, not on-chain, so say that clearly. README says seventeen Python suites, there are eighteen.
- [ ] Replace `print()` with logging (90 calls), add a ruff config and format once, add type checking, remove unused
      imports, fix the `deploy:` comment in the Makefile (audit 2.7).
- [ ] Celo: half-built in `app/`, absent from contracts and web. Finish it or remove it.
- [ ] Faster landing page: fewer RSuite components, split `app.css` (audit 3.7).

## 8. Scaling (after launch, when load calls for it)

- [ ] Postgres plan: 9 phases and 5 decisions, in memory (postgres-plan). Not started.
- [ ] Make nonces and refresh-token rotation single-use for real. They read then delete in two steps, so a second process could redeem one twice. Must come before running several processes.
- [ ] Several API processes, a bundler service, Redis for limits (limits are per process today, so web + Telegram allow double) (audit 4.5).
- [ ] A real queue for chat turns, and a faster path for chain reads (one core handles about 5 swap quotes a second) (audit 4.4, 4.5).
- [ ] Load test on staging with targets set in advance (audit 4.8).

## 9. Parked (ideas and on hold)

- WhatsApp bot: Meta locked the account (error 131031). First read the reason at business.facebook.com/accountquality. Nothing built.
- Local model instead of DeepSeek, and the off-topic gate: waiting on your hardware and model choice. Your note: try
  Runpod, guide at https://docs.runpod.io/agent-setup.md
- ERC-7715 permissions: only if several agents share a wallet, wallets adopt it widely, or users ask for limits like "swaps only".
- Revenue options, none decided: swap fee, premium tier, B2B licence, referrals, points, token (lawyer first).
- Liquidity-position NFT game (your idea): a position holds a mix of tokens; if the mix passes a threshold the user gets paid, otherwise the house keeps it.

## Removed from the old list

- ERC20 to a non-contact: refused by the server (`_resolve_contact`, tested in `test_identity`). Not an on-chain rule.
- How often balances are checked: the dashboard every 60 s and on load, Telegram when asked, and the contract's hook around each session-key transaction.
- QR for the Telegram links: done (`TelegramCard.tsx`).
- Batching UserOps: done (`bundler.Lane`, plus the three-key pool).
- Flashbots explained: search Flashbots in `docs/app.md`. The op goes out through Flashbots' private RPC on mainnet, BlockRazor on BSC.
- The Uniswap V3 fork test plan you pasted: done (`SHUniswapV3ForkTestBase`).
- SHFactory `0x491f...bf50`: an old Sepolia factory. Current Sepolia: `0x6edd9222d0bf361640ee688F60803e9d9Bd74C4D`.
