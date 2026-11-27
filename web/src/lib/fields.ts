/**
 * What a plain input in a form group names in its aria-describedby: its explanation, beside its label
 * (FieldLabel), and its error, under it (FieldError), while it has one.
 */
export const describedBy = (controlId: string, error: unknown) =>
  error ? `${controlId}-help-text ${controlId}-error-text` : `${controlId}-help-text`
