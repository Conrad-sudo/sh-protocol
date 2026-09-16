import { EmptyState } from '../components/EmptyState'
import { PageHeader } from '../components/PageHeader'

/** Stand-in for a section a later phase builds. */
export function PlaceholderPage({ title, summary }: { title: string; summary: string }) {
  return (
    <>
      <PageHeader title={title} />
      <EmptyState title="Coming soon">{summary}</EmptyState>
    </>
  )
}
