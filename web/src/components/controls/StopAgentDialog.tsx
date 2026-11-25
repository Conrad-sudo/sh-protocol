import { Button, Loader, Message, Modal, Text } from 'rsuite'
import { ApiError } from '../../api/client'
import type { AgentStopHandle } from '../../hooks/useAgentStop'
import { agentName } from '../../lib/agents'
import { isPasskeyError } from '../../lib/passkey'
import { errorText } from '../../lib/tx'
import { chainName } from '../../wallet/chains'

/** Why the confirmation couldn't be started, in plain words. */
function startProblem(error: Error) {
  if (error instanceof ApiError && error.status === 409) {
    return 'No passkey you added can approve changes to this wallet. Add this device in Devices above, or remove the agent with your owner wallet.'
  }
  if (error instanceof ApiError && error.status === 404) return 'This has expired. Close and try again.'
  return errorText(error)
}

/** The dialog `useAgentStop` drives: what stopping does, what it costs, and the passkey's Approve. */
export function StopAgentDialog({ stop, chainId }: { stop: AgentStopHandle; chainId: number }) {
  const { agent, open, sending, build, approve, signStart, quote } = stop
  const name = agent ? agentName(agent) : 'this agent'
  const replaced = build.data?.replaced ?? 0
  const problem = approve.error ?? (build.error ? null : signStart.error)

  return (
    <Modal open={open} onClose={stop.close} size="xs" role="alertdialog">
      <Modal.Header>
        <Modal.Title>Stop {name} with your passkey?</Modal.Title>
      </Modal.Header>
      <Modal.Body>
        <Text>
          Your passkey removes {name} from this wallet, so it can't trade from it again. Only your owner wallet can add
          it back.
        </Text>
        {build.isPending ? (
          <Loader className="mf-settings-note" content="Getting it ready…" />
        ) : build.isError ? (
          <Message type="error" showIcon className="mf-settings-note">
            {errorText(build.error)}
          </Message>
        ) : (
          quote && (
            <Text className="mf-settings-note">
              It costs {quote.fees} in fees, paid from the wallet, like any change approved with a passkey.
            </Text>
          )
        )}
        {replaced > 0 && (
          <Text size="sm" muted className="mf-settings-note">
            A payment that was waiting for your approval on {chainName(chainId)} was cancelled to make way for this.
          </Text>
        )}
        {sending && !approve.isError && (
          <Loader className="mf-settings-note" content="Sent. Waiting for the network…" />
        )}
        {problem && (
          <Message type={isPasskeyError(problem, 'cancelled') ? 'info' : 'error'} showIcon className="mf-settings-note">
            {problem === approve.error
              ? isPasskeyError(problem)
                ? problem.message
                : `${name} wasn't stopped: ${errorText(problem)}`
              : startProblem(problem)}
          </Message>
        )}
      </Modal.Body>
      <Modal.Footer className="mf-modal-actions">
        <Button appearance="subtle" onClick={stop.close}>
          {sending ? 'Close' : 'Cancel'}
        </Button>
        <Button
          appearance="primary"
          color="red"
          // Until the first confirmation is ready, so the tap can open the passkey prompt at once.
          loading={approve.isPending || (quote !== null && !signStart.data && !signStart.isError)}
          disabled={!quote || build.isError || !signStart.data}
          onClick={stop.confirm}
        >
          Approve with passkey
        </Button>
      </Modal.Footer>
    </Modal>
  )
}
