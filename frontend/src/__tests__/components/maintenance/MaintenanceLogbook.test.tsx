/**
 * The maintenance logbook: every maintenance done and every calibration run
 * that did not complete, filterable and exportable, and the "done" flow that
 * writes to it with a note.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { MaintenanceLogbook } from '../../../components/maintenance/MaintenanceLogbook';
import { MaintenancePage } from '../../../pages/MaintenancePage';
import { logbookFileName, toCsv } from '../../../components/maintenance/logbookCsv';
import type { MaintenanceLogbookEntry } from '../../../api/client';

function entry(patch: Partial<MaintenanceLogbookEntry>): MaintenanceLogbookEntry {
  return {
    kind: 'performed',
    id: 1,
    at: '2026-09-27T13:42:00Z',
    printer_id: 1,
    printer_name: 'H2S 08',
    maintenance_type_id: 1,
    maintenance_type_name: 'Clean Nozzle/Hotend',
    maintenance_type_icon: null,
    outcome: 'completed',
    source: 'manual',
    trigger: null,
    hours: 5089.4,
    performed_by: 'werkstatt',
    notes: 'hardened 0.4 fitted',
    run_id: null,
    ...patch,
  };
}

const ENTRIES: MaintenanceLogbookEntry[] = [
  entry({}),
  entry({
    id: 2,
    at: '2026-09-27T12:44:00Z',
    printer_id: 2,
    printer_name: 'H2S 09',
    maintenance_type_id: 2,
    maintenance_type_name: 'Printer Calibration',
    source: 'automatic',
    trigger: 'schedule',
    performed_by: null,
    notes: 'Automatic calibration',
    run_id: 14,
  }),
  entry({
    kind: 'run',
    id: 21,
    at: '2026-09-27T13:47:59Z',
    printer_id: 3,
    printer_name: 'H2S 06',
    maintenance_type_id: 3,
    maintenance_type_name: 'Vision Encoder Calibration',
    outcome: 'failed',
    source: 'due',
    hours: null,
    performed_by: null,
    notes: 'Calibration failed (print_error 50348134)',
    run_id: 21,
  }),
];

const PRINTERS = [
  { id: 1, name: 'H2S 08' },
  { id: 2, name: 'H2S 09' },
  { id: 3, name: 'H2S 06' },
];

function rows(): string[] {
  const table = screen.getByRole('table');
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => row.textContent ?? '');
}

describe('logbook CSV', () => {
  it('quotes every field, separates with semicolons and starts with a byte-order mark', () => {
    const csv = toCsv(['Date', 'Note'], [['2026-09-27 15:42', 'said "fine"; next time\nagain']]);

    expect(csv.charCodeAt(0)).toBe(0xfeff);
    expect(csv.slice(1)).toBe('"Date";"Note"\r\n"2026-09-27 15:42";"said ""fine""; next time\nagain"\r\n');
  });

  it('names the file after the printer when filtered to one', () => {
    const day = new Date(2026, 8, 27, 18, 5);
    expect(logbookFileName('wartungsbuch', 'BambuLab H2S 08 LTS3D', day)).toBe(
      'wartungsbuch-bambulab-h2s-08-lts3d-2026-09-27.csv',
    );
    expect(logbookFileName('wartungsbuch', null, day)).toBe('wartungsbuch-2026-09-27.csv');
  });
});

describe('MaintenanceLogbook', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/maintenance/logbook', ({ request }) => {
        const printerId = new URL(request.url).searchParams.get('printer_id');
        const shown = printerId ? ENTRIES.filter((e) => String(e.printer_id) === printerId) : ENTRIES;
        return HttpResponse.json({ entries: shown, total: shown.length });
      }),
    );
  });

  it('lists what was done and what did not complete, saying how and by whom', async () => {
    render(<MaintenanceLogbook printers={PRINTERS} printerId={null} onPrinterChange={vi.fn()} />);

    await screen.findByRole('table');
    const [manual, automatic, failed] = rows();
    expect(manual).toContain('H2S 08');
    expect(manual).toContain('By hand');
    expect(manual).toContain('werkstatt');
    // Hours in the reader's notation — whatever locale the test machine has.
    expect(manual).toContain(`${(5089.4).toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} h`);
    expect(manual).toContain('hardened 0.4 fitted');
    expect(automatic).toContain('Automatic · schedule');
    expect(failed).toContain('Calibration run · when due');
    expect(failed).toContain('Failed');
    expect(failed).toContain('print_error 50348134');
    expect(screen.getByText('3 entries')).toBeInTheDocument();
  });

  it('filters by result, by maintenance and by a search', async () => {
    const user = userEvent.setup();
    render(<MaintenanceLogbook printers={PRINTERS} printerId={null} onPrinterChange={vi.fn()} />);
    await screen.findByRole('table');

    await user.click(screen.getByRole('button', { name: 'Not completed' }));
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toContain('Failed');

    await user.click(screen.getByRole('button', { name: 'All' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Maintenance' }), '2');
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toContain('H2S 09');

    await user.selectOptions(screen.getByRole('combobox', { name: 'Maintenance' }), '');
    await user.type(screen.getByPlaceholderText(/Search notes/), 'werkstatt');
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toContain('H2S 08');
  });

  it('asks the server for one printer and leaves the printer column out', async () => {
    render(<MaintenanceLogbook printers={PRINTERS} printerId={3} onPrinterChange={vi.fn()} />);

    await screen.findByRole('table');
    expect(rows()).toHaveLength(1);
    const header = within(screen.getByRole('table')).getAllByRole('columnheader').map((th) => th.textContent);
    expect(header).not.toContain('Printer');
  });
});

describe('MaintenancePage — marking done', () => {
  let performed: { notes?: string }[];

  beforeEach(() => {
    performed = [];
    server.use(
      http.get('/api/v1/maintenance/types', () => HttpResponse.json([])),
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json([
          {
            printer_id: 1,
            printer_name: 'H2S 08',
            printer_model: 'H2S',
            due_count: 0,
            warning_count: 0,
            total_print_hours: 5100,
            require_plate_clear: false,
            available_actions: [],
            maintenance_items: [
              {
                id: 7,
                printer_id: 1,
                printer_name: 'H2S 08',
                printer_model: 'H2S',
                maintenance_type_id: 1,
                maintenance_type_name: 'Clean Nozzle/Hotend',
                maintenance_type_icon: null,
                maintenance_type_wiki_url: null,
                enabled: true,
                notifications_enabled: true,
                interval_hours: 100,
                interval_type: 'hours',
                current_hours: 5100,
                hours_since_maintenance: 10.6,
                hours_until_due: 89.4,
                days_since_maintenance: null,
                days_until_due: null,
                is_due: false,
                is_warning: false,
                last_performed_at: '2026-09-27T13:42:00Z',
                last_performed_hours_at: 5089.4,
                last_performed_by: 'werkstatt',
                last_performed_notes: 'hardened 0.4 fitted',
                last_performed_source: 'manual',
                action: null,
                trigger_mode: 'manual',
              },
            ],
          },
        ]),
      ),
      http.post('/api/v1/maintenance/items/7/perform', async ({ request }) => {
        performed.push((await request.json()) as { notes?: string });
        return HttpResponse.json({});
      }),
    );
  });

  it('shows when the maintenance was last done, and asks for a note when marking it', async () => {
    const user = userEvent.setup();
    render(<MaintenancePage />);

    await user.click(await screen.findByRole('button', { name: /Expand/ }));
    expect(await screen.findByText(/Last done .* at 5089 h · werkstatt/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^Done$/ }));
    const dialog = await screen.findByRole('dialog', { name: 'Mark as done' });
    await user.type(within(dialog).getByRole('textbox'), 'PTFE tube replaced too');
    await user.click(within(dialog).getByRole('button', { name: 'Enter as done' }));

    await waitFor(() => expect(performed).toEqual([{ notes: 'PTFE tube replaced too' }]));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Mark as done' })).not.toBeInTheDocument());
  });
});
