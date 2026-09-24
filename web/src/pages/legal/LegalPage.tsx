import type { ReactNode } from 'react'
import { Message } from 'rsuite'
import { PageMeta } from '../../components/PageMeta'

/** Where people write about their account or data. Replace before launch. */
export const CONTACT_EMAIL = '[contact email]'

interface LegalPageProps {
  title: string
  /** The page's summary in search results and link previews. */
  description: string
  path: string
  updated: string
  children: ReactNode
}

/**
 * A long-form legal page. Both pages are DRAFT TEMPLATES: they say so on screen until a lawyer has
 * reviewed them, and the banner comes out with that review.
 */
export function LegalPage({ title, description, path, updated, children }: LegalPageProps) {
  return (
    <article className="mf-legal">
      <PageMeta title={`${title} · Mitfah`} description={description} path={path} />
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
