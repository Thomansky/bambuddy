/**
 * Which printers can measure pressure advance by printing.
 *
 * Mirrors the backend's `PA_CALIBRATION_MODELS` so the slot menu can show the
 * action disabled with a reason instead of hiding it — a missing menu entry
 * reads as a Bambuddy bug, while a greyed-out one with "not supported on this
 * printer yet" reads as the truth. The backend is still the authority: the
 * preflight refuses an unsupported model regardless of what this says.
 *
 * H2D / H2D Pro are deliberately absent until the dual-nozzle result shape is
 * measured, and the X1/P1/A1 family uses a different command entirely.
 */
const PA_CALIBRATION_MODELS = new Set(['H2S']);

// Internal model codes the printer may report instead of the display name.
const MODEL_ID_ALIASES: Record<string, string> = {
  O1S: 'H2S',
};

export function supportsPaCalibration(model: string | null | undefined): boolean {
  if (!model) return false;
  const raw = model.trim();
  const resolved = MODEL_ID_ALIASES[raw] ?? raw;
  const normalized = resolved.replace(/^Bambu Lab /i, '').toUpperCase().replace(/[\s-]/g, '');
  return PA_CALIBRATION_MODELS.has(normalized);
}
