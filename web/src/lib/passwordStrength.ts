export const STRENGTH_LABELS = ['Too short', 'Okay', 'Good', 'Strong'] as const

/**
 * A rough 0–3 meter. Below 8 characters (the server's minimum) is 0; length matters most after
 * that, with a little credit for mixed case and for digits or symbols.
 */
export function passwordStrength(password: string): 0 | 1 | 2 | 3 {
  if (password.length < 8) return 0
  const bonus =
    Number(password.length >= 12) +
    Number(/[a-z]/.test(password) && /[A-Z]/.test(password)) +
    Number(/[\d\W_]/.test(password))
  return Math.min(3, 1 + bonus) as 1 | 2 | 3
}
