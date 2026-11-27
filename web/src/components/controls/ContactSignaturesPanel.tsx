import { Text } from 'rsuite'
import { GlassPanel } from '../Glass'
import { TxButton } from '../owner/TxButton'
import { TxStatus } from '../owner/TxStatus'
import { ControlRow } from './ControlRow'
import type { ControlPanelProps } from './types'

/**
 * A passkey's payments go only to contacts the owner signed for (trading agents aren't held to them),
 * from signatures Mitfah keeps. A signature can't be taken back, and removing a contact only stops
 * Mitfah attaching it, so this is how the owner cuts off someone they once added: void, on this
 * network's wallet, every signature made until now, then sign again the contacts to keep. It only ever takes ability away, so it starts without asking first.
 */
export function ContactSignaturesPanel({ tx, locked, start }: ControlPanelProps) {
  return (
    <GlassPanel bordered header={<h2>Contact signatures</h2>} className="mf-card">
      <ControlRow
        title="Cancel old signatures"
        help={
          <Text as="span">
            Payments you approve with a passkey go only to the contacts you signed for. If you no longer trust someone
            you added, cancel every signature made before now on this network, then sign again the contacts you keep,
            in Contacts. Each network's wallet is cancelled separately.
          </Text>
        }
        action={
          <TxButton
            tx={tx}
            txKey="void-contacts"
            appearance="ghost"
            color="red"
            disabled={locked}
            onClick={() =>
              start({
                key: 'void-contacts',
                action: { kind: 'void-contacts' },
                success: 'Old contact signatures cancelled on this network. Sign again the contacts you keep, in Contacts.',
              })
            }
          >
            Cancel old signatures
          </TxButton>
        }
      >
        <TxStatus tx={tx} txKey="void-contacts" />
      </ControlRow>
    </GlassPanel>
  )
}
