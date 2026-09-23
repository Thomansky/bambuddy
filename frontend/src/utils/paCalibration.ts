import { parseUTCDate } from './date';

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

/** Statuses in which a run is still moving. Mirrors ACTIVE_PA_STATUSES minus the wait. */
const LIVE_STATUSES = new Set([
  'queued',
  'slicing',
  'uploading',
  'printing',
  'reading_result',
  'awaiting_confirmation',
  'saving',
]);

/**
 * How long a finished run stays on the printer page.
 *
 * A failed run's whole value is its message — the content guard exists to
 * produce one — and a card that vanishes at the next poll leaves the user
 * with no indication that anything happened. It does not stay forever
 * either: ten minutes is long enough to walk back to the machine, and the
 * card carries a dismiss button for anyone who has read it.
 */
export const TERMINAL_RUN_VISIBLE_MS = 10 * 60 * 1000;

interface RunLike {
  id: number;
  status: string;
  completed_at?: string | null;
  started_at?: string | null;
  created_at?: string | null;
}

/**
 * The run the card should show, out of what the API returned.
 *
 * Live runs always. A terminal one only while it is recent and undismissed:
 * the query cannot ask for `?active=true` any more, because that is exactly
 * the filter that made `done` / `failed` / `cancelled` unreachable in the app.
 */
export function pickVisibleRun<T extends RunLike>(
  runs: T[] | undefined,
  dismissedId: number | null = null,
  now: number = Date.now(),
): T | null {
  const newest = runs?.[0];
  if (!newest) return null;
  if (LIVE_STATUSES.has(newest.status)) return newest;
  if (newest.id === dismissedId) return null;
  const finished = parseUTCDate(newest.completed_at || newest.started_at || newest.created_at);
  if (!finished) return null;
  return now - finished.getTime() <= TERMINAL_RUN_VISIBLE_MS ? newest : null;
}

export function hasLiveRun(runs: RunLike[] | undefined): boolean {
  return (runs ?? []).some((run) => LIVE_STATUSES.has(run.status));
}

/**
 * Why the calibration action is disabled, as an i18n key, or null when it is not.
 *
 * Shared by every entry point on purpose. The slot menu and the K-profile row
 * each computed this themselves, and the slot menu's copy simply left the
 * permission case out — so a user without `printers:control` got a greyed-out
 * button with no explanation anywhere in the card, which reads as a bug rather
 * than as a permission they do not have. One function means the next entry
 * point cannot forget a branch.
 *
 * Order matters: an unsupported model is reported first because none of the
 * other reasons would be worth fixing on that machine.
 */
export function paCalibrationBlockedKey({
  model,
  canControl,
  slotLoaded = true,
}: {
  model: string | null | undefined;
  canControl: boolean;
  slotLoaded?: boolean;
}): string | null {
  if (!supportsPaCalibration(model)) return 'paCalibration.blocked.model_not_supported';
  if (!canControl) return 'paCalibration.noPermission';
  if (!slotLoaded) return 'paCalibration.notLoaded';
  return null;
}
