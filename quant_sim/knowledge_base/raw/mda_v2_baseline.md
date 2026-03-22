# MDA v2.0 A-share baseline

## Role split

- Data architect: summarize market structure, funding flow, valuation, and whether the data is complete enough to support action.
- Knowledge alignment: compare the current question with the project's knowledge base and policy or strategy notes.
- Game psychology: look for crowding, regime change, and whether the market is rewarding or punishing the current style.
- Execution discipline: prefer clear triggers, preserve capital, and reject ambiguous setups.

## Buy threshold

- The system only allows a buy recommendation when the combined score is at least 75 out of 100.
- Each dimension contributes up to 25 points.
- If the knowledge side is weak or unavailable, the safe default is watch rather than force a trade.

## Trading constraints

- Initial simulation capital should be 100,000 RMB.
- Commission uses the current project setting and sells also apply stamp duty.
- A-share T+1 must be respected.
- Newly opened positions are locked for the first 30 minutes and cannot be sold during that window.
- After a position reaches 10 percent profit, trailing protection should tighten and defend gains.
- After a position reaches 15 percent profit, the system should consider a forced partial take-profit.

## Decision style

- Policy rhythm, macro regime, sector leadership, and cash-flow direction should be treated as first-class context.
- A single bullish signal is never enough. The system should prefer aligned signals across market data, knowledge, and execution discipline.
- When the knowledge source is degraded, the assistant must clearly say it is using a fallback path and lower confidence accordingly.
