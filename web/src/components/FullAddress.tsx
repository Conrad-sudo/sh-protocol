/**
 * A whole address in groups of four, so it can be checked character by character before it is
 * trusted. Screen readers get it in one piece.
 */
export function FullAddress({ address }: { address: string }) {
  const groups = address.slice(2).match(/.{1,4}/g) ?? []
  return (
    <span className="mf-full-address">
      <span className="mf-mono mf-full-address-groups" aria-hidden="true">
        <span>0x</span>
        {groups.map((group, i) => (
          <span key={i}>{group}</span>
        ))}
      </span>
      <span className="mf-visually-hidden">{address}</span>
    </span>
  )
}
