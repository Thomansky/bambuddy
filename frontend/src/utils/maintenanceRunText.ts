import type { ActiveMaintenanceRun } from '../api/client';

/**
 * How a calibration run (#3127) reads: why a waiting run has not started yet,
 * and what it needs next. Shared by the Maintenance page's cards and the
 * printer cards.
 */

/** Every waiting_reason the scheduler sets for a run, by its text key. */
export const MAINTENANCE_WAITING_REASON_KEYS: Record<string, string> = {
  printer_offline: 'maintenance.calibration.waitingPrinterOffline',
  printer_busy: 'maintenance.calibration.waitingPrinterBusy',
  awaiting_plate_clear: 'maintenance.calibration.waitingPlateClear',
  already_drying: 'maintenance.calibration.waitingAlreadyDrying',
  bed_too_warm: 'maintenance.calibration.waitingBedTooWarm',
  bed_temp_unknown: 'maintenance.calibration.waitingBedTempUnknown',
  after_other_run: 'maintenance.calibration.waitingAfterOtherRun',
  vision_encoder_plate: 'maintenance.calibration.waitingVisionPlate',
};

export type CalibrationNextStep = 'visionPlate' | 'coolBed' | 'start';

/**
 * What a pending run needs after what it waits for now, following the
 * scheduler's gates: the printer free and its plate released, the bed below
 * the start condition, and for the vision encoder its own plate asked for
 * and released. ``bedTemp`` is the printer's bed right now.
 */
export function nextCalibrationStep(
  run: Pick<ActiveMaintenanceRun, 'action' | 'waiting_reason' | 'plate_requested_at' | 'bed_temp_below'>,
  bedTemp: number | null | undefined,
): CalibrationNextStep {
  const bedWarm = run.bed_temp_below != null && (bedTemp == null || bedTemp >= run.bed_temp_below);
  const plateAhead = run.action === 'motion_precision' && !run.plate_requested_at;
  switch (run.waiting_reason) {
    case 'vision_encoder_plate':
    case 'awaiting_plate_clear':
      return bedWarm ? 'coolBed' : 'start';
    case 'bed_too_warm':
    case 'bed_temp_unknown':
      return plateAhead ? 'visionPlate' : 'start';
    default:
      // Free printer first: after a job the plate gate asks for the vision
      // encoder plate before the bed is looked at.
      if (plateAhead) return 'visionPlate';
      return bedWarm ? 'coolBed' : 'start';
  }
}
