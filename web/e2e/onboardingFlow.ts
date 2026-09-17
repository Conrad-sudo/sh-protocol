import { expect, type Locator, type Page } from '@playwright/test'
import { FAKE_WALLET_NAME } from './fakeWallet.ts'

export const STEPS = {
  connect: 'Connect the wallet that will own your Mitfah wallet',
  verify: 'Prove this wallet is yours',
  network: 'Where should your wallet live?',
  limits: 'How much may the assistant spend?',
  fund: 'Add funds for network fees',
  review: 'Check the details',
} as const

export type StepName = keyof typeof STEPS

interface WalkOptions {
  /** Network tile to pick; the default is whatever the page preselects. */
  network?: RegExp
  /** Changes the limits step's form before continuing. */
  editLimits?: (step: Locator) => Promise<void>
  /** Runs once each step is showing, before anything is clicked (screenshots, layout checks). */
  onStep?: (name: StepName, step: Locator) => Promise<void>
}

/**
 * Walks /onboarding from "Connect wallet" to "Create wallet" with the fake wallet, then stops: the
 * caller decides what a finished deploy should look like.
 */
export async function walkOnboarding(page: Page, { network, editLimits, onStep }: WalkOptions = {}) {
  const step = async (name: StepName) => {
    const region = page.getByRole('region', { name: STEPS[name] })
    await expect(region).toBeVisible()
    await onStep?.(name, region)
    return region
  }
  const next = (region: Locator) => region.getByRole('button', { name: 'Continue' }).click()

  const connect = await step('connect')
  await connect.getByRole('button', { name: 'Connect wallet' }).click()
  await page.getByRole('dialog').getByRole('button', { name: FAKE_WALLET_NAME }).click()

  const verify = await step('verify')
  await verify.getByRole('button', { name: 'Verify with wallet' }).click()

  const networkStep = await step('network')
  if (network) await networkStep.getByText(network).click()
  await next(networkStep)

  const limits = await step('limits')
  // The token list loads separately; Continue stays disabled until it has.
  await expect(limits.getByRole('checkbox').first()).toBeAttached()
  await editLimits?.(limits)
  await next(limits)

  await next(await step('fund'))

  const review = await step('review')
  await review.getByRole('button', { name: 'Create wallet' }).click()
}
