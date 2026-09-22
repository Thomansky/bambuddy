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
import { RFID_REREAD_WAITING_REASON, formatWaitingReason, isBusyOnlyWaitingReason } from '../../utils/waitingReason';

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

  it.each([
    'Maintenance run pending: Printer Calibration (queued)',
    'Scheduled maintenance at Sunday 12:00 — this job would run into it (estimated 3h 20m)',
    'Busy: H2S-01 | Maintenance run pending: Vision Encoder Calibration (bed still warm, 45 °C)',
    'Maintenance run pending: Printer Calibration (queued) — H2S-01, H2S-02',
  ])('reads the maintenance hold %s as waiting its turn (#3127)', reason => {
    // Both resolve by themselves: the run closes, the slot passes. The
    // scheduler files them with the busy-only reasons and so does the UI.
    expect(isBusyOnlyWaitingReason(reason)).toBe(true);
  });
});

describe('formatWaitingReason', () => {
  // A German-speaking t: the keys the two holds use, and the type names.
  const de: Record<string, string> = {
    'queue.maintenanceHold.run': 'Wartungslauf steht an: {{item}} ({{state}})',
    'queue.maintenanceHold.schedule': 'Wartungstermin {{when}} — Auftrag würde hineinlaufen (geschätzt {{duration}})',
    'queue.maintenanceHold.scheduleUnknown': 'Wartungstermin {{when}} — Auftrag würde hineinlaufen (Dauer unbekannt)',
    'queue.maintenanceHold.state.queued': 'eingereiht',
    'queue.maintenanceHold.state.running': 'läuft',
    'queue.maintenanceHold.state.printerBusy': 'Drucker beschäftigt',
    'queue.maintenanceHold.state.bedTooWarm': 'Druckbett noch warm, {{temp}} °C',
    'maintenance.types.printerCalibration': 'Druckerkalibrierung',
    'maintenance.types.visionEncoderCalibration': 'Vision-Encoder-Kalibrierung',
  };
  const t = (key: string, options?: Record<string, unknown>) => {
    let text = de[key] ?? (options?.defaultValue as string | undefined) ?? key;
    for (const [name, value] of Object.entries(options ?? {})) {
      text = text.replace(`{{${name}}}`, String(value));
    }
    return text;
  };

  it('translates a pending-run hold, the item name and the state included', () => {
    expect(formatWaitingReason('Maintenance run pending: Printer Calibration (queued)', t, 'de')).toBe(
      'Wartungslauf steht an: Druckerkalibrierung (eingereiht)',
    );
    expect(
      formatWaitingReason('Maintenance run pending: Vision Encoder Calibration (bed still warm, 45 °C)', t, 'de'),
    ).toBe('Wartungslauf steht an: Vision-Encoder-Kalibrierung (Druckbett noch warm, 45 °C)');
  });

  it('translates a schedule hold with the weekday in the UI language', () => {
    expect(
      formatWaitingReason(
        'Scheduled maintenance at Sunday 12:00 — this job would run into it (estimated 3h 20m)',
        t,
        'de',
      ),
    ).toBe('Wartungstermin Sonntag 12:00 — Auftrag würde hineinlaufen (geschätzt 3h 20m)');
    expect(
      formatWaitingReason(
        'Scheduled maintenance at Saturday 06:00 — this job would run into it (duration unknown)',
        t,
        'de',
      ),
    ).toBe('Wartungstermin Samstag 06:00 — Auftrag würde hineinlaufen (Dauer unbekannt)');
  });

  it('translates each hold inside a joined reason and leaves the rest alone', () => {
    expect(
      formatWaitingReason('Busy: H2S-01 | Maintenance run pending: Printer Calibration (running)', t, 'de'),
    ).toBe('Busy: H2S-01 | Wartungslauf steht an: Druckerkalibrierung (läuft)');
    expect(formatWaitingReason('Waiting for plate confirmation: X1C-01', t, 'de')).toBe(
      'Waiting for plate confirmation: X1C-01',
    );
  });

  it('keeps the printers an "Any <model>" hold names, after the translated sentence', () => {
    // One clause for however many printers are under the same run or slot
    // (#3127): the names are the backend's own and are carried over as written.
    expect(
      formatWaitingReason('Maintenance run pending: Printer Calibration (queued) — H2S-01, H2S-02', t, 'de'),
    ).toBe('Wartungslauf steht an: Druckerkalibrierung (eingereiht) — H2S-01, H2S-02');
    expect(
      formatWaitingReason(
        'Scheduled maintenance at Sunday 12:00 — this job would run into it (duration unknown) — H2S-01, H2S-02',
        t,
        'de',
      ),
    ).toBe('Wartungstermin Sonntag 12:00 — Auftrag würde hineinlaufen (Dauer unbekannt) — H2S-01, H2S-02');
  });

  it('stops the printer list at the clause it belongs to', () => {
    expect(
      formatWaitingReason(
        'Maintenance run pending: Printer Calibration (queued) — H2S-01 | Busy: H2S-03',
        t,
        'de',
      ),
    ).toBe('Wartungslauf steht an: Druckerkalibrierung (eingereiht) — H2S-01 | Busy: H2S-03');
  });

  it('keeps a state or a type name it does not know as the backend wrote it', () => {
    expect(formatWaitingReason('Maintenance run pending: My Cal (some new reason)', t, 'de')).toBe(
      'Wartungslauf steht an: My Cal (some new reason)',
    );
  });
});
