import { shortAddress } from '../lib/format'

/** A shortened address in monospace. The full address is the hover title and the spoken text. */
export function AddressText({ address }: { address: string }) {
  return (
    <span className="mf-mono mf-address" title={address}>
      <span aria-hidden="true">{shortAddress(address)}</span>
      <span className="mf-visually-hidden">{address}</span>
    </span>
  )
}
