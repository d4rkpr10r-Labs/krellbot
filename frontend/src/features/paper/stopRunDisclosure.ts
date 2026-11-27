/**
 * What 'Stop paper run', 'Pause new buys' and 'Close position' do today, said
 * the same way beside every such control (StatusPanel, FleetArmedList,
 * ArmedRecordsList). Button labels and aria-labels stay as they are; controls
 * point at this text with `aria-describedby`.
 *
 * Engine evidence:
 * - Stop paper run is `paper.disarm`: `PaperService.disarm` removes the armed
 *   record only. The paper venue state keeps the base balance and the resting
 *   stop, and `run.tick` evaluates armed records only, so no tick raises or
 *   re-places that stop or acts on an exit signal for it.
 * - Close position is `positions.close`: `run.close_position` (the pair typed
 *   back) cancels the pair's stop, sells only the quantity this strategy owns
 *   and holds new entries with `closed_by_operator_at` until resume.
 * - Pause new buys sets `entries_paused`: `run.tick` suppresses the entry
 *   branch while the exit, stop-repair and `raise_stop` branches still run.
 */

export const STOP_RUN_DISCLOSURE =
  "Stop paper run removes the arm only. Any open paper position and its resting stop stay in place, unmanaged: nothing raises or re-places that stop or acts on an exit signal for it. To sell the position first, use Close position: it cancels the stop, sells only what this strategy owns and pauses new buys.";

export const PAUSE_NEW_BUYS_DISCLOSURE =
  "Pause new buys stops new entries only. Exits, stop repair and stop raises keep running.";

export const CLOSE_POSITION_DISCLOSURE =
  "Close position cancels the resting stop, then sells only the quantity this strategy owns on this pair, never other holdings of the same coin. New buys stay paused until you resume them.";
