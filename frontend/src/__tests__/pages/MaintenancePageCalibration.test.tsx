/**
 * The Printer Calibration card on the Maintenance page (#3127): option
 * checkboxes, trigger and schedule controls, run status and the Run now /
 * Cancel buttons wired to the runs API. Also the per-item notification bell
 * every card carries and the gate hint on the automatic trigger options.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { render } from '../utils';
import { MaintenancePage } from '../../pages/MaintenancePage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import type { MaintenanceStatus } from '../../api/client';
import i18n from '../../i18n';

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
  notifications_enabled: true,
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
  reserve_before_schedule: true,
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

function overviewWith(item: Partial<MaintenanceStatus>, extra: MaintenanceStatus[] = [], requirePlateClear = false) {
  return [
    {
      printer_id: 1,
      printer_name: 'H2S',
      printer_model: 'H2S',
      total_print_hours: 40,
      due_count: 0,
      warning_count: 0,
      maintenance_items: [{ ...baseItem, ...item }, reminderItem, ...extra],
      require_plate_clear: requirePlateClear,
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

  it('a scheduled item offers the keep-clear checkbox, which round-trips the flag', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith({
            trigger_mode: 'schedule',
            schedule_days: [6],
            schedule_time: '12:00',
            schedule_next_at: '2026-09-27T10:00:00Z',
            reserve_before_schedule: true,
          })
        )
      )
    );
    const panel = await expandPrinter();
    const box = within(panel).getByLabelText("Don't start jobs that would run into the scheduled time");
    expect(box).toBeChecked();
    fireEvent.click(box);
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ reserve_before_schedule: false });
  });

  it('the keep-clear checkbox reflects a flag that is off, and is absent without a schedule', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith(
            {
              trigger_mode: 'schedule',
              schedule_days: [6],
              schedule_time: '12:00',
              schedule_next_at: '2026-09-27T10:00:00Z',
              reserve_before_schedule: false,
            },
            [{ ...motionItem, trigger_mode: 'when_due', reserve_before_schedule: true }]
          )
        )
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).getByLabelText("Don't start jobs that would run into the scheduled time")).not.toBeChecked();
    // The vision encoder item is on "when due": nothing to keep clear of
    const motionPanel = screen.getByTestId('calibration-panel-9');
    expect(within(motionPanel).queryByLabelText("Don't start jobs that would run into the scheduled time")).toBeNull();
  });

  it('a run behind another one on the same printer says whose turn it is', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith(
            {
              current_run: { id: 46, status: 'running', source: 'schedule', waiting_reason: null, waiting_detail: null, started_at: '2026-09-27T10:00:00Z' },
            },
            [
              {
                ...motionItem,
                current_run: { id: 47, status: 'pending', source: 'schedule', waiting_reason: 'after_other_run', waiting_detail: { item: 'Printer Calibration' }, started_at: null },
              },
            ]
          )
        )
      )
    );
    await expandPrinter();
    const motionPanel = screen.getByTestId('calibration-panel-9');
    expect(within(motionPanel).getByText('Waiting: after Printer Calibration')).toBeInTheDocument();
  });

  it('names the run ahead under its translated name (German)', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith(
            {},
            [
              {
                ...motionItem,
                current_run: { id: 47, status: 'pending', source: 'schedule', waiting_reason: 'after_other_run', waiting_detail: { item: 'Printer Calibration' }, started_at: null },
              },
            ]
          )
        )
      )
    );
    await expandPrinter();
    const motionPanel = screen.getByTestId('calibration-panel-9');
    await i18n.changeLanguage('de');
    try {
      await waitFor(() =>
        expect(within(motionPanel).getByText('Wartet: nach Druckerkalibrierung')).toBeInTheDocument()
      );
    } finally {
      await i18n.changeLanguage('en');
    }
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
            current_run: { id: 42, status: 'pending', source: 'schedule', waiting_reason: 'awaiting_plate_clear', waiting_detail: null, started_at: null },
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
            current_run: { id: 43, status: 'running', source: 'manual', waiting_reason: null, waiting_detail: null, started_at: '2026-09-19T05:02:00Z' },
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
            last_run: { id: 40, printer_maintenance_id: 7, printer_id: 1, status: 'failed', source: 'due', options: {}, start_after: null, waiting_reason: null, waiting_detail: null, error_message: 'Calibration failed (print_error 83886081)', created_at: '2026-09-18T05:00:00Z', started_at: '2026-09-18T05:01:00Z', completed_at: '2026-09-18T05:20:00Z' },
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
    // The only checkbox is the shared bed-temperature condition
    expect(within(panel).getAllByRole('checkbox')).toHaveLength(1);
    expect(within(panel).queryByLabelText('Bed leveling')).toBeNull();
    expect(within(panel).getByRole('combobox', { name: 'Trigger' })).toHaveValue('manual');
    const runNow = within(panel).getByRole('button', { name: /Run now/ });
    expect(runNow).toBeEnabled();
    fireEvent.click(runNow);
    await waitFor(() => expect(runCalls).toBe(1));
    // ...while the bed-levelling card next to it still has its options
    expect(within(screen.getByTestId('calibration-panel-7')).getByLabelText('Bed leveling')).toBeInTheDocument();
  });

  it('ticking the bed condition adds bed_temp_below at 30; unticking removes the key', async () => {
    const panel = await expandPrinter();
    const box = within(panel).getByLabelText('Only when the bed is below');
    expect(box).not.toBeChecked();
    expect(within(panel).queryByLabelText('Bed temperature threshold (°C)')).toBeNull();
    fireEvent.click(box);
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({
      action_options: { bed_leveling: true, vibration: true, motor_noise: true, bed_temp_below: 30 },
    });
  });

  it('a language whose sentence continues after the value renders the suffix (German)', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(overviewWith({ action_options: { bed_leveling: true, vibration: true, motor_noise: true, bed_temp_below: 30 } }))
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).queryByText('ist')).toBeNull();
    await i18n.changeLanguage('de');
    try {
      await waitFor(() => expect(within(panel).getByLabelText('Nur wenn das Druckbett unter')).toBeChecked());
      expect(within(panel).getByText('°C')).toBeInTheDocument();
      expect(within(panel).getByText('ist')).toBeInTheDocument();
    } finally {
      await i18n.changeLanguage('en');
    }
  });

  it('the threshold input round-trips and unticking drops the key while keeping the flags', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(overviewWith({ action_options: { bed_leveling: true, vibration: true, motor_noise: true, bed_temp_below: 28.5 } }))
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).getByLabelText('Only when the bed is below')).toBeChecked();
    const input = within(panel).getByLabelText('Bed temperature threshold (°C)');
    expect(input).toHaveValue(28.5);
    expect(within(panel).getByText('°C')).toBeInTheDocument();

    fireEvent.change(input, { target: { value: '35' } });
    fireEvent.blur(input);
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({
      action_options: { bed_leveling: true, vibration: true, motor_noise: true, bed_temp_below: 35 },
    });

    // An out-of-range value is not sent; the field snaps back
    fireEvent.change(input, { target: { value: '500' } });
    fireEvent.blur(input);
    expect(patches).toHaveLength(1);
    expect(input).toHaveValue(28.5);

    // Toggling a flag keeps the condition
    fireEvent.click(within(panel).getByLabelText('Nozzle clumping detection'));
    await waitFor(() => expect(patches).toHaveLength(2));
    expect(patches[1]).toEqual({
      action_options: { bed_leveling: true, vibration: true, motor_noise: true, bed_temp_below: 28.5, nozzle_clumping: true },
    });

    fireEvent.click(within(panel).getByLabelText('Only when the bed is below'));
    await waitFor(() => expect(patches).toHaveLength(3));
    expect(patches[2]).toEqual({ action_options: { bed_leveling: true, vibration: true, motor_noise: true } });
  });

  it('the vision encoder item offers the bed condition without any flags', async () => {
    server.use(http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overviewWith({}, [motionItem]))));
    await expandPrinter();
    const panel = await screen.findByTestId('calibration-panel-9');
    fireEvent.click(within(panel).getByLabelText('Only when the bed is below'));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ action_options: { bed_temp_below: 30 } });
  });

  it('a run waiting for the bed shows the temperature; an unknown one says so', async () => {
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json(
          overviewWith(
            {
              current_run: { id: 44, status: 'pending', source: 'due', waiting_reason: 'bed_too_warm', waiting_detail: { bed_temp: 34.2, threshold: 30 }, started_at: null },
            },
            [
              {
                ...motionItem,
                current_run: { id: 45, status: 'pending', source: 'manual', waiting_reason: 'bed_temp_unknown', waiting_detail: null, started_at: null },
              },
            ]
          )
        )
      )
    );
    const panel = await expandPrinter();
    expect(within(panel).getByText('Waiting: bed still warm (34.2 °C)')).toBeInTheDocument();
    const motionPanel = await screen.findByTestId('calibration-panel-9');
    expect(within(motionPanel).getByText('Waiting: bed temperature unknown')).toBeInTheDocument();
  });

  it('names the idle gate on the automatic triggers while plate-clear confirmation is off', async () => {
    const panel = await expandPrinter();
    const select = within(panel).getByRole('combobox', { name: 'Trigger' });
    expect(within(select).getByRole('option', { name: 'When due (once the printer is idle)' })).toHaveValue('when_due');
    expect(within(select).getByRole('option', { name: 'On a schedule (once the printer is idle)' })).toHaveValue('schedule');
    expect(within(select).getByRole('option', { name: 'Manual' })).toHaveValue('manual');
  });

  it('names the plate-clear gate on the automatic triggers while the setting is on', async () => {
    server.use(http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overviewWith({}, [], true))));
    const panel = await expandPrinter();
    const select = within(panel).getByRole('combobox', { name: 'Trigger' });
    expect(
      within(select).getByRole('option', { name: 'When due (only once the plate has been released)' })
    ).toHaveValue('when_due');
    expect(
      within(select).getByRole('option', { name: 'On a schedule (only once the plate has been released)' })
    ).toHaveValue('schedule');
    // The plain label is still what the German status line and others use
    expect(within(select).queryByRole('option', { name: 'When due' })).toBeNull();
  });

  it('every card carries the bell, and a reminder-only item can be muted from it', async () => {
    let muted = false;
    let answer!: () => void;
    const answered = new Promise<void>((resolve) => {
      answer = resolve;
    });
    server.use(
      http.get('/api/v1/maintenance/overview', () =>
        HttpResponse.json([
          { ...overviewWith({})[0], maintenance_items: [baseItem, { ...reminderItem, notifications_enabled: !muted }] },
        ])
      ),
      http.patch('/api/v1/maintenance/items/8', async ({ request }) => {
        patches.push(await request.json());
        muted = true;
        await answered;
        return HttpResponse.json({ ...reminderItem, notifications_enabled: false });
      })
    );
    await expandPrinter();
    // Two cards, two bells, both on
    const bells = screen.getAllByRole('button', { name: 'Notifications on' });
    expect(bells).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Notifications off' })).toBeNull();
    expect(bells[0]).toHaveAttribute('aria-pressed', 'true');

    // The reminder-only card (Clean Build Plate) has one too
    const reminderCard = screen.getByText('Clean Build Plate').closest('div.rounded-xl')!;
    const bell = within(reminderCard).getByRole('button', { name: 'Notifications on' });
    expect(bell).toHaveAttribute('title', 'Notifications on');
    fireEvent.click(bell);

    // Optimistic: the bell has flipped while the PATCH is still unanswered
    await waitFor(() =>
      expect(within(reminderCard).getByRole('button', { name: 'Notifications off' })).toHaveAttribute('aria-pressed', 'false')
    );
    // ...and only that item's bell
    const calibrationCard = screen.getByTestId('calibration-panel-7').closest('div.rounded-xl')!;
    expect(within(calibrationCard).getByRole('button', { name: 'Notifications on' })).toBeInTheDocument();

    answer();
    await waitFor(() => expect(patches).toEqual([{ notifications_enabled: false }]));
    // Still muted once the server has answered and the overview was re-read
    await waitFor(() => expect(muted).toBe(true));
    expect(within(reminderCard).getByRole('button', { name: 'Notifications off' })).toBeInTheDocument();
  });

  it('a muted item renders the slashed bell and un-mutes with one click', async () => {
    server.use(http.get('/api/v1/maintenance/overview', () => HttpResponse.json(overviewWith({ notifications_enabled: false }))));
    const panel = await expandPrinter();
    const card = panel.closest('div.rounded-xl')!;
    const bell = within(card).getByRole('button', { name: 'Notifications off' });
    expect(bell).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(bell);
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ notifications_enabled: true });
  });

  it('a failed PATCH puts the bell back before the refetch answers', async () => {
    // The overview is held once the bell is clicked, so the only way back to
    // "on" while it hangs is the mutation's own rollback, not the invalidation.
    let answerPatch!: () => void;
    const patchAnswered = new Promise<void>((resolve) => {
      answerPatch = resolve;
    });
    let answerOverview!: () => void;
    const overviewAnswered = new Promise<void>((resolve) => {
      answerOverview = resolve;
    });
    let holdOverview = false;
    let overviewAnswers = 0;
    server.use(
      http.get('/api/v1/maintenance/overview', async () => {
        if (holdOverview) await overviewAnswered;
        overviewAnswers += 1;
        return HttpResponse.json(overviewWith({}));
      }),
      http.patch('/api/v1/maintenance/items/8', async () => {
        await patchAnswered;
        return HttpResponse.json({ detail: 'nope' }, { status: 500 });
      })
    );
    await expandPrinter();
    const reminderCard = screen.getByText('Clean Build Plate').closest('div.rounded-xl')!;
    const answersBeforeClick = overviewAnswers;
    holdOverview = true;
    fireEvent.click(within(reminderCard).getByRole('button', { name: 'Notifications on' }));
    await waitFor(() =>
      expect(within(reminderCard).getByRole('button', { name: 'Notifications off' })).toBeInTheDocument()
    );
    answerPatch();
    await waitFor(() =>
      expect(within(reminderCard).getByRole('button', { name: 'Notifications on' })).toBeInTheDocument()
    );
    expect(overviewAnswers).toBe(answersBeforeClick);
    answerOverview();
    await waitFor(() => expect(overviewAnswers).toBeGreaterThan(answersBeforeClick));
    expect(within(reminderCard).getByRole('button', { name: 'Notifications on' })).toBeInTheDocument();
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
