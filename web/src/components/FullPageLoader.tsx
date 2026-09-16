import { Loader } from 'rsuite'

export function FullPageLoader() {
  return (
    <div className="mf-fullpage">
      <Loader size="md" content="Loading…" vertical />
    </div>
  )
}
