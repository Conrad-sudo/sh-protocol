import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router'
import { Button, Form, Loader, Message, PasswordInput, Schema, Text } from 'rsuite'
import { login, type Credentials } from '../api/auth'
import { ApiError } from '../api/client'
import { googleEnabled } from '../auth/google'
import { useAuth } from '../auth/useAuth'
import { GoogleButton } from '../components/GoogleButton'
import { OrDivider } from '../components/OrDivider'
import { PageMeta } from '../components/PageMeta'
import { useGoogleSignIn } from '../hooks/useGoogleSignIn'

const { StringType } = Schema.Types

const model = Schema.Model({
  email: StringType().isEmail('Enter a valid email address').isRequired('Enter your email'),
  password: StringType().isRequired('Enter your password'),
})

function errorMessage(error: unknown) {
  if (!(error instanceof ApiError)) return 'Something went wrong. Please try again.'
  if (error.status === 401) return 'Incorrect email or password.'
  return error.message
}

/**
 * Email + password or Google sign-in. On success the session starts, and RedirectIfSignedIn (around
 * this page) sends the user on to `?next=` or the dashboard.
 */
export function LoginPage() {
  const { signIn } = useAuth()
  const [params] = useSearchParams()
  const [value, setValue] = useState<Credentials>({ email: params.get('email') ?? '', password: '' })
  const submit = useMutation({ mutationFn: login, onSuccess: signIn })
  const google = useGoogleSignIn()

  const next = params.get('next')
  const signupLink = next ? `/signup?next=${encodeURIComponent(next)}` : '/signup'

  return (
    <>
      <PageMeta
        title="Sign in · Mitfah"
        description="Sign in to Mitfah to manage your wallet, change its limits and talk to your assistant."
        path="/login"
      />
      <h1 className="mf-auth-title">Welcome back</h1>
      <Text muted>Sign in to manage your wallet and talk to your assistant.</Text>

      {submit.isError && (
        <Message type="error" showIcon style={{ marginTop: 20 }}>
          {errorMessage(submit.error)}
        </Message>
      )}
      {google.error && (
        <Message type="error" showIcon style={{ marginTop: 20 }}>
          {google.error.message}
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
          <Form.Control name="password" accepter={PasswordInput} autoComplete="current-password" />
        </Form.Group>
        <Button appearance="primary" type="submit" block size="lg" loading={submit.isPending}>
          Sign in
        </Button>
      </Form>

      {googleEnabled && (
        <>
          <OrDivider />
          {google.pending ? (
            <Loader center={false} content="Signing in with Google…" />
          ) : (
            <GoogleButton
              text="signin_with"
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
        New to Mitfah? <Link to={signupLink}>Create an account</Link>
      </Text>
    </>
  )
}
