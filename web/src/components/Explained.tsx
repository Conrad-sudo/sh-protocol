import type { ReactNode } from 'react'
import { Form, useFormGroup } from 'rsuite'

/*
 * Every explanation sits beside what it explains, on the same line, and wraps under it like a
 * paragraph: "Name: So you can tell them apart." The colon is only drawn: screen readers hear the
 * heading or label, then the explanation.
 */

const Colon = () => (
  <span className="mf-explained-colon" aria-hidden="true">
    :
  </span>
)

interface ExplainedProps {
  /** 1 for a page, 2 for a card, 3 for a section or a row; 'label' for a setting's name, not a heading. */
  level: 1 | 2 | 3 | 'label'
  title: ReactNode
  /** A state shown between the title and its explanation, such as "Active". */
  status?: ReactNode
  /** The explanation, if there is one. */
  children?: ReactNode
  /** For the heading itself, such as the page title's class. */
  titleClassName?: string
  /** The heading's id, for a section's aria-labelledby. */
  titleId?: string
}

/** A heading with its explanation beside it: "Access token: Gives an agent access to…" */
export function Explained({ level, title, status, children, titleClassName, titleId }: ExplainedProps) {
  const Heading = level === 'label' ? 'strong' : (`h${level}` as const)
  const explained = status || children
  return (
    <div className="mf-explained">
      <Heading id={titleId} className={titleClassName}>
        {title}
        {explained && <Colon />}
      </Heading>
      {status}
      {children && <span className="mf-explained-text">{children}</span>}
    </div>
  )
}

/**
 * A field's label with its explanation beside it. The explanation keeps RSuite's help-text id
 * (`<controlId>-help-text`), which a plain input names in its aria-describedby.
 */
export function FieldLabel({ children, help }: { children: ReactNode; help?: ReactNode }) {
  return (
    <div className="mf-explained">
      <Form.Label>{children}</Form.Label>
      {help && (
        <>
          <Colon />
          <Form.Text>{help}</Form.Text>
        </>
      )}
    </div>
  )
}

/** What's wrong with a field, under its input, as `<controlId>-error-text`. */
export function FieldError({ children }: { children: ReactNode }) {
  const { controlId } = useFormGroup()
  return (
    <Form.Text id={`${controlId}-error-text`} className="mf-error-text">
      {children}
    </Form.Text>
  )
}
