/**
 * The "Maintenance Run Finished" event toggle on NotificationProviderCard
 * (#3127): its badge in the summary strip and the PATCH the toggle sends,
 * separate from the Maintenance Due toggle next to it.
 */

import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import type { NotificationProvider } from '../../api/client';

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

function buildProvider(overrides: Partial<NotificationProvider> = {}): NotificationProvider {
  return {
    id: 1,
    name: 'Test Provider',
    provider_type: 'ntfy',
    enabled: true,
    config: { server: 'https://ntfy.sh', topic: 'bambuddy' },
    on_print_start: false,
    on_print_complete: false,
    on_print_failed: false,
    on_print_stopped: false,
    on_print_progress: false,
    on_print_missing_spool_assignment: false,
    on_printer_offline: false,
    on_printer_error: false,
    on_ai_failure_detection: false,
    on_filament_low: false,
    on_maintenance_due: false,
    on_maintenance_run: false,
    on_ams_humidity_high: false,
    on_ams_temperature_high: false,
    on_ams_ht_humidity_high: false,
    on_ams_ht_temperature_high: false,
    on_plate_not_empty: false,
    on_bed_cooled: false,
    on_first_layer_complete: false,
    on_queue_job_added: false,
    on_queue_job_assigned: false,
    on_queue_job_started: false,
    on_queue_job_waiting: false,
    on_queue_job_skipped: false,
    on_queue_job_failed: false,
    on_queue_completed: false,
    on_stock_reorder_alert: false,
    on_stock_break_alert: false,
    quiet_hours_enabled: false,
    quiet_hours_start: null,
    quiet_hours_end: null,
    daily_digest_enabled: false,
    daily_digest_time: null,
    printer_id: null,
    last_success: null,
    last_error: null,
    last_error_at: null,
    created_at: '2026-04-25T00:00:00Z',
    updated_at: '2026-04-25T00:00:00Z',
    ...overrides,
  };
}

describe('NotificationProviderCard — Maintenance Run Finished (#3127)', () => {
  it('shows the badge only when the event is on', async () => {
    render(<NotificationProviderCard provider={buildProvider({ on_maintenance_run: true })} onEdit={vi.fn()} />);
    expect(await screen.findByText('Maintenance Run Finished')).toBeInTheDocument();
  });

  it('shows no badge for a provider that predates the event', async () => {
    render(<NotificationProviderCard provider={buildProvider()} onEdit={vi.fn()} />);
    await screen.findByText('Test Provider');
    expect(screen.queryByText('Maintenance Run Finished')).not.toBeInTheDocument();
  });

  it('offers the toggle under Printer Status, next to Maintenance Due', async () => {
    const user = userEvent.setup();
    render(
      <NotificationProviderCard
        provider={buildProvider({ on_maintenance_due: true, on_maintenance_run: false })}
        onEdit={vi.fn()}
      />,
    );
    await user.click(await screen.findByText(/event settings/i));

    const section = (await screen.findByText('Printer Status')).closest('div')!;
    const dueRow = within(section).getByText('Maintenance Due').closest('div.flex')!;
    const runRow = within(section).getByText('Maintenance Run Finished').closest('div.flex')!;
    expect(within(runRow).getByText(/calibration run Bambuddy queued/)).toBeInTheDocument();
    expect(within(dueRow).getByRole('switch')).toHaveAttribute('aria-checked', 'true');
    expect(within(runRow).getByRole('switch')).toHaveAttribute('aria-checked', 'false');
  });

  it('toggling it PATCHes on_maintenance_run alone', async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(
      http.patch('*/api/v1/notifications/1', async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json(buildProvider({ on_maintenance_run: true }));
      }),
    );

    const user = userEvent.setup();
    render(<NotificationProviderCard provider={buildProvider()} onEdit={vi.fn()} />);
    await user.click(await screen.findByText(/event settings/i));

    const runRow = (await screen.findByText('Maintenance Run Finished')).closest('div.flex')!;
    await user.click(within(runRow).getByRole('switch'));

    await waitFor(() => expect(captured).not.toBeNull());
    expect(captured).toEqual({ on_maintenance_run: true });
  });
});
