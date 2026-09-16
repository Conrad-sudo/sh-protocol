import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { Button, Form, Loader, Message, PasswordInput, PasswordStrengthMeter, Schema, Text } from 'rsuite'
import { signup, type Credentials } from '../api/auth'
import { ApiError } from '../api/client'
import { googleEnabled } from '../auth/google'
import { useAuth } from '../auth/useAuth'
import { GoogleButton } from '../components/GoogleButton'
import { OrDivider } from '../components/OrDivider'
import { useGoogleSignIn } from '../hooks/useGoogleSignIn'
import { passwordStrength, STRENGTH_LABELS } from '../lib/passwordStrength'

const { StringType } = Schema.Types

// Mirrors SignupRequest in app/api.py: 8 is the floor, 1024 the cap.
const model = Schema.Model({
  email: StringType().isEmail('Enter a valid email address').isRequired('Enter your email'),
  password: StringType()
    .minLength(8, 'Use at least 8 characters')
    .maxLength(1024, 'That password is too long')
    .isRequired('Choose a password'),
})

export function SignupPage() {
  const { signIn } = useAuth()
  const [params] = useSearchParams()
  const [value, setValue] = useState<Credentials>({ email: params.get('email') ?? '', password: '' })
  const submit = useMutation({ mutationFn: signup, onSuccess: signIn })
  const google = useGoogleSignIn()

  const next = params.get('next')
  const loginLink = (email?: string) => {
    const query = new URLSearchParams()
    if (email) query.set('email', email)
    if (next) query.set('next', next)
    const search = query.toString()
    return search ? `/login?${search}` : '/login'
  }

  const level = passwordStrength(value.password)
  const error = submit.error

  return (
    <>
      <title>Create account · Mitfah</title>
      <h1 className="mf-auth-title">Create your account</h1>
      <Text muted>Then deploy a wallet you own and set how much the assistant may spend.</Text>

      {submit.isError && (
        <Message type="error" showIcon style={{ marginTop: 20 }}>
          {error instanceof ApiError && error.status === 409 ? (
            <>
              An account with that email already exists.{' '}
              <Link to={loginLink(value.email)}>Sign in instead</Link>
            </>
          ) : error instanceof ApiError ? (
            error.message
          ) : (
            'Something went wrong. Please try again.'
          )}
        </Message>
      )}
      {google.error && (
        <Message type="error" showIcon style={{ marginTop: 20 }}>
          {google.error.message}{' '}
          {google.error.emailTaken && <Link to={loginLink()}>Sign in with your password</Link>}
        </Message>
      )}

      <Form
        fluid
        className="mf-auth-form"
        model={model}
        formValue={value}
        onChange={formValue => setValue(formValue as Credentials)}
        onSubmit={formValue => {
          google.reset()
          submit.mutate(formValue as Credentials)
        }}
      >
        <Form.Group controlId="email">
          <Form.Label>Email</Form.Label>
          <Form.Control name="email" type="email" autoComplete="email" />
        </Form.Group>
        <Form.Group controlId="password">
          <Form.Label>Password</Form.Label>
          <Form.Control name="password" accepter={PasswordInput} autoComplete="new-password" />
          {value.password && (
            <PasswordStrengthMeter level={level} label={STRENGTH_LABELS[level]} style={{ marginTop: 8 }} />
          )}
        </Form.Group>
        <Button appearance="primary" type="submit" block size="lg" loading={submit.isPending}>
          Create account
        </Button>
      </Form>

      {googleEnabled && (
        <>
          <OrDivider />
          {google.pending ? (
            <Loader center={false} content="Creating your account with Google…" />
          ) : (
            <GoogleButton
              text="signup_with"
              onCredential={token => {
                submit.reset()
                google.onCredential(token)
              }}
              onFailure={google.onFailure}
            />
          )}
        </>
      )}

      <Text className="mf-auth-alt">
        Already have an account? <Link to={loginLink()}>Sign in</Link>
      </Text>
    </>
  )
}
