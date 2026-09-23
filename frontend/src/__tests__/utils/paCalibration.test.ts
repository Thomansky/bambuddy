/**
 * The frontend's copy of the flow-dynamics allow-list.
 *
 * It mirrors the backend's PA_CALIBRATION_MODELS so the slot menu can show the
 * action disabled with a reason rather than hiding it. The backend stays the
 * authority — these expectations exist so widening one list without the other
 * is a failing test rather than a menu entry that leads to a 400.
 */

import { describe, it, expect } from 'vitest';
import {
  hasLiveRun,
  paCalibrationBlockedKey,
  pickVisibleRun,
  supportsPaCalibration,
} from '../../utils/paCalibration';

describe('supportsPaCalibration', () => {
  it.each(['H2S', 'Bambu Lab H2S', 'h2s', 'O1S'])('accepts %s', (model) => {
    expect(supportsPaCalibration(model)).toBe(true);
  });

  // H2D is one constant away but deliberately absent: the dual-nozzle result
  // shape is unmeasured, and guessing the extruder writes a correct K value to
  // the wrong nozzle. X1/P1/A1 calibrate through a different MQTT command.
  it.each(['H2D', 'H2D Pro', 'X1C', 'P1S', 'A1', 'A1 mini', '', null, undefined])(
    'refuses %s',
    (model) => {
      expect(supportsPaCalibration(model)).toBe(false);
    },
  );
});

describe('pickVisibleRun', () => {
  const NOW = Date.parse('2026-09-20T12:00:00Z');

  function row(overrides: Record<string, unknown> = {}) {
    return {
      id: 7,
      status: 'printing',
      completed_at: null,
      started_at: '2026-09-20T11:55:00',
      created_at: '2026-09-20T11:54:00',
      ...overrides,
    };
  }

  it('shows a live run', () => {
    expect(pickVisibleRun([row()], null, NOW)?.id).toBe(7);
  });

  // The bug this exists for: the card asked the API for `?active=true`, which
  // the route filters to ACTIVE_PA_STATUSES, so a failed run and its carefully
  // composed message simply vanished at the next poll.
  it('keeps a run that just failed, with its message still reachable', () => {
    const failed = row({ status: 'failed', completed_at: '2026-09-20T11:59:00' });
    expect(pickVisibleRun([failed], null, NOW)?.status).toBe('failed');
  });

  it.each(['done', 'cancelled'])('keeps a %s run that just ended', (status) => {
    expect(pickVisibleRun([row({ status, completed_at: '2026-09-20T11:59:30' })], null, NOW)?.status).toBe(status);
  });

  it('lets go of one that ended long ago', () => {
    const stale = row({ status: 'failed', completed_at: '2026-09-20T11:00:00' });
    expect(pickVisibleRun([stale], null, NOW)).toBeNull();
  });

  it('lets go of one the user dismissed', () => {
    const failed = row({ status: 'failed', completed_at: '2026-09-20T11:59:00' });
    expect(pickVisibleRun([failed], 7, NOW)).toBeNull();
  });

  it('never hides a live run, dismissed or not', () => {
    expect(pickVisibleRun([row()], 7, NOW)?.id).toBe(7);
  });

  it('has nothing to show for an empty list', () => {
    expect(pickVisibleRun([], null, NOW)).toBeNull();
    expect(pickVisibleRun(undefined, null, NOW)).toBeNull();
  });

  it('reads the backend timestamp as UTC, not as local time', () => {
    // Naive ISO from the backend. Read as local time in a positive-offset
    // zone it lands in the future and the card would show a stale run
    // forever; in a negative one it would never show a fresh one at all.
    const failed = row({ status: 'failed', completed_at: '2026-09-20T11:59:00' });
    expect(pickVisibleRun([failed], null, Date.parse('2026-09-20T23:00:00Z'))).toBeNull();
  });
});

describe('hasLiveRun', () => {
  it('is what keeps the poll slow on an idle printer', () => {
    expect(hasLiveRun([{ id: 1, status: 'failed' }])).toBe(false);
    expect(hasLiveRun([{ id: 1, status: 'failed' }, { id: 2, status: 'queued' }])).toBe(true);
    expect(hasLiveRun(undefined)).toBe(false);
  });
});

describe('paCalibrationBlockedKey', () => {
  it('is silent when nothing blocks', () => {
    expect(paCalibrationBlockedKey({ model: 'H2S', canControl: true })).toBeNull();
  });

  // The slot menu computed this itself and left the permission case out, so a
  // read-only user got a greyed-out button with no explanation anywhere.
  it('names the missing permission rather than saying nothing', () => {
    expect(paCalibrationBlockedKey({ model: 'H2S', canControl: false })).toBe('paCalibration.noPermission');
  });

  it('reports the model first, because nothing else would be worth fixing', () => {
    expect(paCalibrationBlockedKey({ model: 'X1C', canControl: false })).toBe(
      'paCalibration.blocked.model_not_supported',
    );
  });

  it('reports an unloaded filament last', () => {
    expect(paCalibrationBlockedKey({ model: 'H2S', canControl: true, slotLoaded: false })).toBe(
      'paCalibration.notLoaded',
    );
  });
});
