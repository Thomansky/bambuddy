/**
 * Reading the scheduler's `waiting_reason` (#3074).
 *
 * The backend writes one sentence per held queue item and encodes one bit in
 * its shape: a reason made only of `Busy: ...` clauses means the job starts by
 * itself once a printer frees up, and anything else means somebody has to do
 * something — load filament, switch a printer on, confirm a plate. The
 * scheduler uses that bit to decide whether a hold is worth a notification
 * (`PrintScheduler._is_busy_only`), and the UI needs the same distinction to
 * decide whether a held item still belongs on a forecast.
 *
 * The two maintenance holds (#3127) — a calibration run pending on the
 * printer, a scheduled one the job would run into — count as busy-only on
 * both sides: they resolve themselves. They are also the only reasons the
 * queue row translates: the backend writes them in a fixed English shape
 * (`maintenance_actions.queue_hold_for_run` / `queue_hold_for_schedule`,
 * on an "Any <model>" row followed by " — " and the printers it is about,
 * `queue_hold_clauses`) and `formatWaitingReason` maps that shape onto the
 * locale's own sentence, the printer names carried over as written.
 *
 * Kept in one place on this side too, so the halves of the contract are one
 * grep apart.
 */

import { maintenanceTypeLabel } from './maintenanceTypeLabels';

type TFunction = (key: string, options?: Record<string, unknown>) => string;

const RUN_HOLD_PREFIX = 'Maintenance run pending: ';
const SCHEDULE_HOLD_PREFIX = 'Scheduled maintenance at ';

/** Is this clause one of the two maintenance holds? */
export function isMaintenanceHold(clause: string): boolean {
  return clause.startsWith(RUN_HOLD_PREFIX) || clause.startsWith(SCHEDULE_HOLD_PREFIX);
}

/** Does this reason mean "waiting its turn", rather than "waiting for you"? */
export function isBusyOnlyWaitingReason(reason: string | null | undefined): boolean {
  if (!reason) return false;
  return reason
    .split(' | ')
    .map(part => part.trim())
    .every(part => part.startsWith('Busy:') || isMaintenanceHold(part));
}

// The run's state as the backend phrases it inside the hold, keyed to the
// locale's own phrase. "bed still warm" carries the temperature after a comma.
const RUN_STATE_KEYS: Record<string, string> = {
  queued: 'queued',
  running: 'running',
  'printer offline': 'printerOffline',
  'printer busy': 'printerBusy',
  'plate not released yet': 'plateClear',
  'AMS drying in progress': 'alreadyDrying',
  'bed still warm': 'bedTooWarm',
  'bed temperature unknown': 'bedTempUnknown',
};

const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

// A clause ends at the string's end or at one of the scheduler's joiners
// (" | " between busy-only clauses, "; " between labelled ones), optionally
// after the printers the hold is about.
const PRINTERS = '(?: — (.+?))?';
const CLAUSE_END = '(?=$| \\| |; )';
const RUN_HOLD_RE = new RegExp(`${RUN_HOLD_PREFIX}(.+?) \\(([^()]*)\\)${PRINTERS}${CLAUSE_END}`, 'g');
const SCHEDULE_HOLD_RE = new RegExp(
  `${SCHEDULE_HOLD_PREFIX}(${WEEKDAYS.join('|')}) (\\d{2}:\\d{2}) — this job would run into it ` +
    `\\((?:estimated (.+?)|duration unknown)\\)${PRINTERS}${CLAUSE_END}`,
  'g',
);

function withPrinters(text: string, printers?: string): string {
  return printers ? `${text} — ${printers}` : text;
}

// 2024-01-01 is a Monday; a local date so the label is not shifted by the zone.
function weekdayName(index: number, language: string): string {
  return new Intl.DateTimeFormat(language, { weekday: 'long' }).format(new Date(2024, 0, 1 + index));
}

/**
 * The reason as the queue row shows it: the maintenance holds in the UI
 * language, everything else as the backend wrote it.
 */
export function formatWaitingReason(reason: string, t: TFunction, language: string): string {
  return reason
    .replace(RUN_HOLD_RE, (_match, item: string, state: string, printers?: string) => {
      const bed = /^bed still warm, (.+) °C$/.exec(state);
      const key = RUN_STATE_KEYS[bed ? 'bed still warm' : state];
      const stateText = key ? t(`queue.maintenanceHold.state.${key}`, { temp: bed?.[1] }) : state;
      return withPrinters(
        t('queue.maintenanceHold.run', { item: maintenanceTypeLabel(item, t), state: stateText }),
        printers,
      );
    })
    .replace(SCHEDULE_HOLD_RE, (_match, weekday: string, time: string, duration?: string, printers?: string) => {
      const when = `${weekdayName(WEEKDAYS.indexOf(weekday), language)} ${time}`;
      const text = duration
        ? t('queue.maintenanceHold.schedule', { when, duration })
        : t('queue.maintenanceHold.scheduleUnknown', { when });
      return withPrinters(text, printers);
    });
}
