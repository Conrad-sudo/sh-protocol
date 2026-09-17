import type { ReactNode } from 'react'
import { Message } from 'rsuite'

/** Where people write about their account or data. Replace before launch. */
export const CONTACT_EMAIL = '[contact email]'

interface LegalPageProps {
  title: string
  updated: string
  children: ReactNode
}

/**
 * A long-form legal page. Both pages are DRAFT TEMPLATES: they say so on screen until a lawyer has
 * reviewed them, and the banner comes out with that review.
 */
export function LegalPage({ title, updated, children }: LegalPageProps) {
  return (
    <article className="mf-legal">
      <title>{`${title} · Mitfah`}</title>
      <h1>{title}</h1>
      <p className="mf-legal-updated">Last updated {updated}</p>
      <Message type="warning" showIcon className="mf-legal-draft">
        <strong>Draft.</strong> This page is a template that hasn't been reviewed by a lawyer yet. It will change
        before Mitfah launches.
      </Message>
      {children}
    </article>
  )
}
