import { useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchPasskeys, fetchRegistrationOptions, registerPasskey } from '../api/passkeys'
import type { Passkey, PasskeyRegistrationJSON, RegisteredPasskey } from '../api/types'
import {
  createPasskey,
  devicePasskeysSnapshot,
  findDevicePasskey,
  isPasskeyError,
  parseDevicePasskeys,
  PasskeyError,
  subscribeDevicePasskeys,
} from '../lib/passkey'

export const PASSKEYS_KEY = ['passkeys'] as const
export const PASSKEY_OPTIONS_KEY = ['passkey-options'] as const

/**
 * How long fetched registration options are used for. The server keeps a challenge a little longer;
 * a tap on older ones would be refused, so they are fetched again while the page waits.
 */
const OPTIONS_FRESH_MS = 50_000

/** The account's passkeys, on every device. */
export function usePasskeys() {
  return useQuery({ queryKey: PASSKEYS_KEY, queryFn: fetchPasskeys })
}

/** Credential ids this browser holds, as far as it knows (lib/passkey.ts), kept current. */
export function useDevicePasskeyIds(): string[] {
  return parseDevicePasskeys(useSyncExternalStore(subscribeDevicePasskeys, devicePasskeysSnapshot, () => ''))
}

/** The account's passkey that is on this device, or null when this browser knows of none. */
export function useDevicePasskey(): Passkey | null {
  const { data: passkeys } = usePasskeys()
  const ids = useDevicePasskeyIds()
  return passkeys?.find(passkey => ids.includes(passkey.credential_id)) ?? null
}

/**
 * Options to make a passkey with, fetched as soon as the user means to make one, so the tap that
 * makes it can open the browser's prompt at once. Each set holds a single-use challenge, so they are
 * renewed while they wait and dropped once the server has seen them.
 */
export function useRegistrationOptions(enabled: boolean) {
  return useQuery({
    queryKey: PASSKEY_OPTIONS_KEY,
    queryFn: ({ signal }) => fetchRegistrationOptions(signal),
    enabled,
    staleTime: OPTIONS_FRESH_MS,
    refetchInterval: enabled ? OPTIONS_FRESH_MS : false,
    gcTime: 0,
    retry: false,
  })
}

/** The options fetched by useRegistrationOptions, if they are there and fresh enough to tap with now. */
export function freshRegistrationOptions(queryClient: ReturnType<typeof useQueryClient>) {
  const state = queryClient.getQueryState<PublicKeyCredentialCreationOptionsJSON>(PASSKEY_OPTIONS_KEY)
  if (!state?.data || state.fetchStatus === 'fetching') return null
  return Date.now() - state.dataUpdatedAt < OPTIONS_FRESH_MS ? state.data : null
}

interface RegisterVariables {
  /** The browser's prompt, opened by the tap (createPasskey). */
  pending: Promise<PasskeyRegistrationJSON>
  deviceName: string
}

interface FindVariables {
  /** The browser's prompt, opened by the tap (findDevicePasskey). */
  pending: Promise<string>
  passkeys: Passkey[]
}

/**
 * Makes this device's passkey from a tap, or finds the account's passkey it already holds, then hands
 * it to `onReady`. Each prompt opens inside the tap that calls `make` or `findExisting`, and only
 * then do the requests that need its answer run. The mutations run whatever the browser thinks of
 * its connection: the prompt is already open, so its answer must reach them.
 */
export function useDevicePasskeyMaker(onReady: (passkey: Passkey) => void) {
  const queryClient = useQueryClient()
  const register = useMutation({
    mutationFn: async ({ pending, deviceName }: RegisterVariables): Promise<RegisteredPasskey> =>
      registerPasskey(await pending, deviceName.trim()),
    networkMode: 'always',
    onSuccess: saved => {
      queryClient.setQueryData<Passkey[]>(PASSKEYS_KEY, list => [
        ...(list ?? []).filter(passkey => passkey.key_id !== saved.key_id),
        saved,
      ])
      void queryClient.invalidateQueries({ queryKey: PASSKEYS_KEY })
    },
    onSettled: (_saved, error) => {
      // The server has seen the challenge (it is single use), unless the browser never made a passkey.
      if (!isPasskeyError(error)) queryClient.removeQueries({ queryKey: PASSKEY_OPTIONS_KEY })
    },
  })
  const find = useMutation({
    mutationFn: async ({ pending, passkeys }: FindVariables) => {
      const credentialId = await pending
      const found = passkeys.find(passkey => passkey.credential_id === credentialId)
      if (!found) throw new PasskeyError('failed', "That passkey isn't one of yours on Mitfah.")
      return found
    },
    networkMode: 'always',
  })

  /** Opens the prompt to make a passkey called `deviceName`. Call it straight from the tap. */
  const make = (deviceName: string) => {
    register.reset()
    find.reset()
    const options = freshRegistrationOptions(queryClient)
    const pending = options
      ? createPasskey(options)
      : // Not fetched in time: fetched now, after which a browser may refuse to open the prompt
        // outside the tap. The options are then ready for the next one.
        queryClient
          .fetchQuery({ queryKey: PASSKEY_OPTIONS_KEY, queryFn: () => fetchRegistrationOptions(), staleTime: 0 })
          .then(createPasskey)
          .catch((error: unknown) => {
            if (isPasskeyError(error, 'cancelled')) {
              throw new PasskeyError('cancelled', 'Your browser needs one more tap. Tap the button again.')
            }
            throw error
          })
    register.mutate({ pending, deviceName }, { onSuccess: onReady })
  }

  /** Opens the prompt that finds which of `passkeys` this device holds. Call it straight from the tap. */
  const findExisting = (passkeys: Passkey[]) => {
    register.reset()
    find.reset()
    const pending = findDevicePasskey(passkeys.map(passkey => passkey.credential_id))
    find.mutate({ pending, passkeys }, { onSuccess: onReady })
  }

  return {
    make,
    findExisting,
    making: register.isPending,
    finding: find.isPending,
    error: register.error ?? find.error,
  }
}
