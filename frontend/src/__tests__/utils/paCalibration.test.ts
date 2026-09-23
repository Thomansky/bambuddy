/**
 * The frontend's copy of the flow-dynamics allow-list.
 *
 * It mirrors the backend's PA_CALIBRATION_MODELS so the slot menu can show the
 * action disabled with a reason rather than hiding it. The backend stays the
 * authority — these expectations exist so widening one list without the other
 * is a failing test rather than a menu entry that leads to a 400.
 */

import { describe, it, expect } from 'vitest';
import { supportsPaCalibration } from '../../utils/paCalibration';

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
