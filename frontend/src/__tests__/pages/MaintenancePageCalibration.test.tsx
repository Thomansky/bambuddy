/**
 * The Printer Calibration card on the Maintenance page (#3127): option
 * checkboxes, trigger and schedule controls, run status and the Run now /
 * Cancel buttons wired to the runs API.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';
import { MaintenancePage } from '../../pages/MaintenancePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { MaintenanceStatus } from '../../api/client';

const baseItem: MaintenanceStatus = {
  id: 7,
  printer_id: 1,
  printer_name: 'H2S',
  printer_model: 'H2S',
  maintenance_type_id: 10,
  maintenance_type_name: 'Printer Calibration',
  maintenance_type_icon: 'Target',
  maintenance_type_wiki_url: null,
  enabled: true,
  interval_hours: 100,
  interval_type: 'hours',
  current_hours: 40,
  hours_since_maintenance: 40,
  hours_until_due: 60,
  days_since_maintenance: null,
  days_until_due: null,
  is_due: false,
  is_warning: false,
  last_performed_at: null,
  action: 'calibration',
  action_options: { bed_leveling: true, vibration: true, motor_noise: true },
  action_available_options: ['bed_leveling', 'vibration', 'motor_noise', 'high_temp_heatbed', 'micro_lidar', 'nozzle_clumping'],
  trigger_mode: 'manual',
  schedule_days: null,
  schedule_time: null,
  schedule_next_at: null,
  current_run: null,
  last_run: null,
};

const reminderItem: MaintenanceStatus = {
  ...baseItem,
  id: 8,
  maintenance_type_id: 11,
  maintenance_type_name: 'Clean Build Plate',
  action: null,
  action_options: null,
  action_available_options: null,
};

// The H2 series' vision encoder calibration: an action without options.
const motionItem: MaintenanceStatus = {
  ...baseItem,
  id: 9,
  maintenance_type_id: 12,
  maintenance_type_name: 'Vision Encoder Calibration',
  maintenance_type_icon: 'ScanEye',
  interval_hours: 7,
  interval_type: 'days',
  days_since_maintenance: 8,
  days_until_due: -1,
  is_due: true,
  action: 'motion_precision',
  action_options: null,
  action_available_options: null,
};

function overviewWith(item: Partial<MaintenanceStatus>, extra: MaintenanceStatus[] = []) {
  return [
    {
      printer_id: 1,
      printer_name: 'H2S',
      printer_model: 'H2S',
      total_print_hours: 40,
      due_count: 0,
      warning_count: 0,
      maintenance_items: [{ ...baseItem, ...item }, reminderItem, ...extra],
    },
  ];
}

async function expandPrinter() {
  render(<MaintenancePage />);
  const expand = await screen.findByRole('button', { name: /expand/i });
  fireEvent.click(expand);
  return await screen.findByTestId('calibration-panel-7');
}

describe('MaintenancePage calibration card', () => {
  let patches: unknown[];
  let runCalls: number;
  let cancelledRunIds: string[];

  beforeEach(() => {
    patches = [];
    runCalls = 0;
    cancelledRunIds = [];
    server.use(
      http.get('/api/v1/maintenance/types', () =>
        HttpResponse.json([
          { id: 10, name: 'Printer Calibration', description: '', default_interval_hours: 100, interval_type: 'hours', icon: 'Target', wiki_url: null, is_system: true, action: 'calibration', created_at: '2026-01-01T00:00:00Z' },
          { id: 11, name: 'Clean Build Plate', description: '', default_interval_hours: 25, interval_type: 'hours', icon: 'Square', wiki_url: null, is_system: true, action: null, created_at: '2026-01-01T00:00:00Z' },
        ])
      ),
      http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overviewWith({}))),
      http.patch('/api/v1/maintenance/items/:id', async ({ request }) => {
        patches.push(await request.json());
        return HttpResponse.json({ ...baseItem });
      }),
      http.post('/api/v1/maintenance/items/:id/run', () => {
        runCalls += 1;
        return HttpResponse.json({ id: 99, printer_maintenance_id: 7, printer_id: 1, status: 'pending', source: 'manual', options: {}, start_after: null, waiting_reason: null, error_message: null, created_at: '2026-09-19T05:00:00Z', started_at: null, completed_at: null });
      }),
      http.delete('/api/v1/maintenance/runs/:id', ({ params }) => {
        cancelledRunIds.push(String(params.id));
        return HttpResponse.json({ status: 'cancelled', id: Number(params.id) });
      })
    );
  });

  it('shows the option checkboxes the printer model supports, and the trigger select', async () => {
    const panel = await expandPrinter();
    expect(within(panel).getByLabelText('Bed leveling')).toBeChecked();
    expect(within(panel).getByLabelText('Vibration compensation')).toBeChecked();
    expect(within(panel).getByLabelText('Nozzle clumping detection')).not.toBeChecked();
    // H2S is single-nozzle: no nozzle offset offered
    expect(within(panel).queryByLabelText('Nozzle offset')).toBeNull();
    expect(within(panel).getByRole('combobox', { name: 'Trigger' })).toHaveValue('manual');
    // Reminder-only items get no panel
    expect(screen.queryByTestId('calibration-panel-8')).toBeNull();
  });

  it('toggling an option PATCHes the full option set', async () => {
    const panel = await expandPrinter();
    fireEvent.click(within(panel).getByLabelText('Nozzle clumping detection'));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({
      action_options: { bed_leveling: true, vibration: true, motor_noise: true, nozzle_clumping: true },
    });
  });

  it('choosing the schedule trigger sends default weekday and time together', async () => {
    const panel = await expandPrinter();
    fireEvent.change(within(panel).getByRole('combobox', { name: 'Trigger' }), { target: { value: 'schedule' } });
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ trigger_mode: 'schedule', schedule_days: [5], schedule_time: '06:00' });
  });

  it('shows weekday chips and the time input for a scheduled item, with the next run', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith({
            trigger_mode: 'schedule',
            schedule_days: [5, 6],
            schedule_time: '06:00',
            schedule_next_at: '2026-09-26T04:00:00Z',
          })
        )
      )
    );
    const panel = await expandPrinter();
    const days = within(panel).getByRole('group', { name: 'Weekdays' });
    const chips = within(days).getAllByRole('button');
    expect(chips).toHaveLength(7);
    expect(chips[5]).toHaveAttribute('aria-pressed', 'true');
    expect(chips[6]).toHaveAttribute('aria-pressed', 'true');
    expect(chips[0]).toHaveAttribute('aria-pressed', 'false');
    expect(within(panel).getByLabelText('Earliest time')).toHaveValue('06:00');
    expect(within(panel).getByText(/Next run:/)).toBeInTheDocument();

    fireEvent.click(chips[0]);
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ schedule_days: [0, 5, 6] });
  });

  it('Run now calls the run endpoint', async () => {
    const panel = await expandPrinter();
    const runNow = within(panel).getByRole('button', { name: /Run now/ });
    expect(runNow).toBeEnabled();
    fireEvent.click(runNow);
    await waitFor(() => expect(runCalls).toBe(1));
    expect(await screen.findByText('Calibration queued')).toBeInTheDocument();
  });

  it('a pending run shows its waiting reason, disables Run now and offers Cancel', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith({
            current_run: { id: 42, status: 'pending', source: 'schedule', waiting_reason: 'awaiting_plate_clear', started_at: null },
          })
        )
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).getByText('Waiting: plate not released yet')).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: /Run now/ })).toBeDisabled();
    fireEvent.click(within(panel).getByRole('button', { name: /Cancel run/ }));
    await waitFor(() => expect(cancelledRunIds).toEqual(['42']));
  });

  it('a running run says since when; a failed last run shows its error', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith({
            current_run: { id: 43, status: 'running', source: 'manual', waiting_reason: null, started_at: '2026-09-19T05:02:00Z' },
          })
        )
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).getByText(/Calibration running since/)).toBeInTheDocument();
  });

  it('shows the last result when nothing is queued', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith({
            last_run: { id: 40, printer_maintenance_id: 7, printer_id: 1, status: 'failed', source: 'due', options: {}, start_after: null, waiting_reason: null, error_message: 'Calibration failed (print_error 83886081)', created_at: '2026-09-18T05:00:00Z', started_at: '2026-09-18T05:01:00Z', completed_at: '2026-09-18T05:20:00Z' },
          })
        )
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).getByText(/Last run failed .*83886081/)).toBeInTheDocument();
  });

  it('badges the calibration type on the settings tab', async () => {
    render(<MaintenancePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Settings' }));
    expect(await screen.findByText('Runs a calibration')).toBeInTheDocument();
  });

  it('renders the vision encoder item without the option row but with the shared controls', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overviewWith({}, [motionItem]))),
      http.post('/api/v1/maintenance/items/9/run', () => {
        runCalls += 1;
        return HttpResponse.json({ id: 100, printer_maintenance_id: 9, printer_id: 1, status: 'pending', source: 'manual', options: null, start_after: null, waiting_reason: null, error_message: null, created_at: '2026-09-19T05:00:00Z', started_at: null, completed_at: null });
      })
    );
    await expandPrinter();
    const panel = await screen.findByTestId('calibration-panel-9');
    expect(within(panel).queryByRole('checkbox')).toBeNull();
    expect(within(panel).queryByLabelText('Bed leveling')).toBeNull();
    expect(within(panel).getByRole('combobox', { name: 'Trigger' })).toHaveValue('manual');
    const runNow = within(panel).getByRole('button', { name: /Run now/ });
    expect(runNow).toBeEnabled();
    fireEvent.click(runNow);
    await waitFor(() => expect(runCalls).toBe(1));
    // ...while the bed-levelling card next to it still has its options
    expect(within(screen.getByTestId('calibration-panel-7')).getByLabelText('Bed leveling')).toBeInTheDocument();
  });

  it('badges every actionable type on the settings tab', async () => {
    server.use(
      http.get('/api/v1/maintenance/types', () =>
        HttpResponse.json([
          { id: 10, name: 'Printer Calibration', description: '', default_interval_hours: 100, interval_type: 'hours', icon: 'Target', wiki_url: null, is_system: true, action: 'calibration', created_at: '2026-01-01T00:00:00Z' },
          { id: 12, name: 'Vision Encoder Calibration', description: '', default_interval_hours: 7, interval_type: 'days', icon: 'ScanEye', wiki_url: null, is_system: true, action: 'motion_precision', created_at: '2026-01-01T00:00:00Z' },
          { id: 11, name: 'Clean Build Plate', description: '', default_interval_hours: 25, interval_type: 'hours', icon: 'Square', wiki_url: null, is_system: true, action: null, created_at: '2026-01-01T00:00:00Z' },
        ])
      )
    );
    render(<MaintenancePage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Settings' }));
    expect(await screen.findAllByText('Runs a calibration')).toHaveLength(2);
  });
});
