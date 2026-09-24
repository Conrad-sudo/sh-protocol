import { useState } from 'react'
import BlockRoundIcon from '@rsuite/icons/BlockRound'
import CheckRoundIcon from '@rsuite/icons/CheckRound'
import ReloadIcon from '@rsuite/icons/Reload'
import { Button } from 'rsuite'
import { LimitDial } from '../LimitDial'

/*
 * The landing page's one moving moment: a day with a $100 limit, played once. Two payments go
 * through and the dial runs down; a third would cross the limit, and the wallet refuses it. The
 * timing lives in CSS (.mf-demo), so the page renders the finished day for anyone who asks for
 * reduced motion, and for scrapers and tests.
 */
const DAY = [
  { ask: 'Pay Sam 20 USDC', answer: 'Sent. $80 left today.', refused: false },
  { ask: 'Pay the landlord 50 USDC', answer: 'Sent. $30 left today.', refused: false },
  { ask: 'Pay Alex 45 USDC', answer: 'Refused by your wallet. That would go over today’s $100.', refused: true },
]

export function LimitDemo() {
  // Remounting the day restarts its CSS animations.
  const [run, setRun] = useState(0)

  return (
    <section className="mf-demo" aria-labelledby="demo-heading">
      <div className="mf-demo-plate" key={run}>
        <LimitDial percent={30} label="30% of the $100 limit left" size={260} className="mf-demo-dial">
          <span className="mf-demo-amounts">
            <strong className="mf-dial-amount mf-num" data-step="0" aria-hidden>
              $100
            </strong>
            <strong className="mf-dial-amount mf-num" data-step="1" aria-hidden>
              $80
            </strong>
            <strong className="mf-dial-amount mf-num" data-step="2">
              $30
            </strong>
          </span>
          <small>left of $100</small>
        </LimitDial>

        <div className="mf-demo-log">
          <h2 id="demo-heading">A day with a $100 limit</h2>
          <ol>
            {DAY.map((line, i) => (
              <li key={line.ask} data-step={i} data-refused={line.refused}>
                <p className="mf-demo-ask">{line.ask}</p>
                <p className="mf-demo-answer">
                  {line.refused ? <BlockRoundIcon aria-hidden /> : <CheckRoundIcon aria-hidden />}
                  {line.answer}
                </p>
              </li>
            ))}
          </ol>
          <Button appearance="subtle" size="sm" startIcon={<ReloadIcon />} onClick={() => setRun(r => r + 1)} className="mf-demo-replay">
            Play again
          </Button>
        </div>
      </div>
    </section>
  )
}
