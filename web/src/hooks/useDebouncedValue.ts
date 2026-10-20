import { useEffect, useState } from 'react'

/** `value`, once it has stopped changing for `ms`: for work worth doing only when typing pauses. */
export function useDebouncedValue<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}
