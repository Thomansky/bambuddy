/**
 * The frontend half of the `waiting_reason` shape contract (#3074).
 *
 * The scheduler encodes "starts by itself" as a reason made only of `Busy: ...`
 * clauses, joined with ` | `, and decides notifications on that basis. The UI
 * reads the same bit to decide what still belongs on a forecast. These cases
 * are the exact strings both branches of the scheduler emit, so a reword on
 * either side lands here first.
 */

import { describe, it, expect } from 'vitest';
import { RFID_REREAD_WAITING_REASON, isBusyOnlyWaitingReason } from '../../utils/waitingReason';

describe('isBusyOnlyWaitingReason', () => {
  it('treats nothing as not-busy rather than busy', () => {
    // An item with no reason is dispatchable, not "waiting its turn"; callers
    // check the reason's presence separately.
    expect(isBusyOnlyWaitingReason(null)).toBe(false);
    expect(isBusyOnlyWaitingReason(undefined)).toBe(false);
    expect(isBusyOnlyWaitingReason('')).toBe(false);
  });

  it.each([
    'Busy: X1C-01',
    'Busy: X1C-01 (drying)',
    'Busy: X1C-01, X1C-02',
    'Busy: X1C-01 | Busy: X1C-02',
  ])('reads %s as waiting its turn', reason => {
    expect(isBusyOnlyWaitingReason(reason)).toBe(true);
  });

  it.each([
    'Waiting for plate confirmation: X1C-01',
    'Offline, no Auto On smart plug: X1C-01',
    'Offline: X1C-01 — the smart plug could not power it on',
    'Waiting on Enclosure Door',
    'Waiting for filament: X1C-01 (needs PETG)',
    'No available X1C printers',
    'Every file for this job has been deleted — add a file back or remove the item',
  ])('reads %s as waiting for the user', reason => {
    expect(isBusyOnlyWaitingReason(reason)).toBe(false);
  });

  it('reads the pre-dispatch RFID read as waiting its turn', () => {
    // The scheduler holds the item for one pass while the printer reads its
    // unidentified AMS slots, then dispatches it by itself. Nobody has to do
    // anything, so it belongs on the forecast like a Busy: hold does.
    expect(RFID_REREAD_WAITING_REASON).toBe('rfid_reread');
    expect(isBusyOnlyWaitingReason('rfid_reread')).toBe(true);
    expect(isBusyOnlyWaitingReason('Busy: X1C-01 | rfid_reread')).toBe(true);
  });

  it('needs every clause to be busy, not just the first', () => {
    // The scheduler only joins with " | " when every clause is busy, but the
    // reader must not assume that -- one clause needing the user makes the
    // whole reason need the user.
    expect(isBusyOnlyWaitingReason('Busy: X1C-01 | Offline: X1C-02')).toBe(false);
  });
});
