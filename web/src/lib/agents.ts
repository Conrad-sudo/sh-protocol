import type { Agent } from '../api/types'
import { shortAddress } from './format'

/** What the owner calls an agent: its name, or its short address when it has none. */
export function agentName(agent: Pick<Agent, 'name' | 'address'>) {
  return agent.name ? `“${agent.name}”` : shortAddress(agent.address)
}
