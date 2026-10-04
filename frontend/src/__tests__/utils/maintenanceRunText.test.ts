/**
 * What a waiting calibration run (#3127) needs next, following the
 * scheduler's gates: printer free and plate released, bed cool enough, and
 * for the vision encoder its own plate.
 */

import { describe, it, expect } from 'vitest';
import { nextCalibrationStep } from '../../utils/maintenanceRunText';

const vision = { action: 'motion_precision' as const, plate_requested_at: null, bed_temp_below: 33 };
const levelling = { action: 'calibration' as const, plate_requested_at: null, bed_temp_below: null };

describe('nextCalibrationStep', () => {
  it('after the plate comes the bed, while it is still warm', () => {
    const asked = { ...vision, plate_requested_at: '2026-10-04T10:51:49Z', waiting_reason: 'vision_encoder_plate' };
    expect(nextCalibrationStep(asked, 40)).toBe('coolBed');
    expect(nextCalibrationStep(asked, 30)).toBe('start');
    expect(nextCalibrationStep(asked, null)).toBe('coolBed');
  });

  it('after the bed comes the plate, unless it was asked for already', () => {
    expect(nextCalibrationStep({ ...vision, waiting_reason: 'bed_too_warm' }, 36)).toBe('visionPlate');
    expect(
      nextCalibrationStep({ ...vision, waiting_reason: 'bed_too_warm', plate_requested_at: '2026-10-04T10:49:17Z' }, 36)
    ).toBe('start');
  });

  it('behind another run or a busy printer the vision encoder plate comes first', () => {
    expect(nextCalibrationStep({ ...vision, waiting_reason: 'after_other_run' }, 60)).toBe('visionPlate');
    expect(nextCalibrationStep({ ...vision, waiting_reason: 'printer_busy' }, 60)).toBe('visionPlate');
  });

  it('a levelling run without a condition just starts', () => {
    expect(nextCalibrationStep({ ...levelling, waiting_reason: 'awaiting_plate_clear' }, 60)).toBe('start');
    expect(nextCalibrationStep({ ...levelling, waiting_reason: null }, 60)).toBe('start');
    expect(nextCalibrationStep({ ...levelling, bed_temp_below: 30, waiting_reason: 'printer_busy' }, 45)).toBe('coolBed');
  });
});
