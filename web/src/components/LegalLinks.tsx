import { Link } from 'react-router'

/** Terms and Privacy links, for the public footer and under the sign-in card. */
export function LegalLinks() {
  return (
    <nav className="mf-legal-links" aria-label="Legal">
      <Link to="/terms">Terms</Link>
      <Link to="/privacy">Privacy</Link>
    </nav>
  )
}
