import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const components: Components = {
  // Links leave the app in a new tab, with no way back to it.
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
  // Never loaded: a reply can quote text other people wrote (an agent's registration file, say),
  // and an image URL is fetched the moment it renders, which would leak whatever it carries.
  img: ({ alt }) => (alt ? <span>[image: {alt}]</span> : null),
  // Wide tables scroll inside the message, not the page.
  table: ({ node: _node, ...props }) => (
    <div className="mf-md-table">
      <table {...props} />
    </div>
  ),
}

/**
 * An assistant reply. Raw HTML in the text is shown as text, never run, and react-markdown drops
 * unsafe link schemes such as javascript:.
 */
export function ChatMarkdown({ text }: { text: string }) {
  return (
    <div className="mf-md">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </Markdown>
    </div>
  )
}
