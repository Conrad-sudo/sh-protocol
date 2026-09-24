/** Where the site lives. Canonical and share links must name it even when built or run elsewhere. */
export const SITE_URL = 'https://mitfah.com'

interface PageMetaProps {
  title: string
  description: string
  /** The page's own address, e.g. `/terms`. */
  path: string
}

/**
 * A public page's title, search-result description and canonical address, and the same for link
 * previews. React 19 hoists these into <head>. The share image and card type are site-wide and stay
 * in index.html; X reads og:title and og:description when its own tags are absent.
 */
export function PageMeta({ title, description, path }: PageMetaProps) {
  const url = SITE_URL + path
  return (
    <>
      <title>{title}</title>
      <meta name="description" content={description} />
      <link rel="canonical" href={url} />
      <meta property="og:url" content={url} />
      <meta property="og:title" content={title} />
      <meta property="og:description" content={description} />
    </>
  )
}
