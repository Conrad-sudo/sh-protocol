import { Input, InputGroup } from 'rsuite'

interface AmountInputProps {
  value: string
  onChange: (value: string) => void
  /** Shown after the number, e.g. "ETH". */
  unit?: string
  /** When given, a "Max" button fills it in. */
  max?: string
  id?: string
  disabled?: boolean
  'aria-label'?: string
}

/**
 * A decimal amount kept as a string, so "0.1" stays exactly 0.1 on its way to the API. Commas typed
 * on some keyboards become dots.
 */
export function AmountInput({ value, onChange, unit, max, id, disabled, ...rest }: AmountInputProps) {
  return (
    <InputGroup>
      <Input
        id={id}
        inputMode="decimal"
        autoComplete="off"
        placeholder="0.0"
        value={value}
        disabled={disabled}
        aria-label={rest['aria-label']}
        onChange={next => onChange(next.replace(',', '.').replace(/[^\d.]/g, ''))}
      />
      {max !== undefined && (
        <InputGroup.Button onClick={() => onChange(max)} disabled={disabled}>
          Max
        </InputGroup.Button>
      )}
      {unit && <InputGroup.Addon>{unit}</InputGroup.Addon>}
    </InputGroup>
  )
}
