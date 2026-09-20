/**
 * Printer wear on the Statistics page (#694).
 *
 * The Quick Stats "Wear & Tear" tile is opt-in by nature — it only exists once
 * some printer has a wear rate and a run has accrued wear — and the
 * "costliest print" record adds depreciation to filament and energy.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { StatsPage } from '../../pages/StatsPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const baseStats = {
  total_prints: 2,
  successful_prints: 2,
  failed_prints: 0,
  cancelled_prints: 0,
  total_print_time_hours: 5,
  total_filament_grams: 100,
  total_cost: 3.0,
  prints_by_filament_type: { PLA: 2 },
  prints_by_printer: { '1': 2 },
  average_time_accuracy: null,
  time_accuracy_by_printer: null,
  total_energy_kwh: 0,
  total_energy_cost: 0,
  total_depreciation_cost: 0,
};

const runs = [
  {
    id: 1,
    created_at: '2024-01-01T10:00:00Z',
    started_at: '2024-01-01T10:00:00Z',
    completed_at: '2024-01-01T12:00:00Z',
    print_name: 'Cheap filament, long run',
    status: 'completed',
    printer_id: 1,
    filament_type: 'PLA',
    filament_used_grams: 20,
    actual_time_seconds: 7200,
    print_time_seconds: 7200,
    cost: 1.0,
    energy_cost: null,
    depreciation_cost: 1.5,
    quantity: 1,
  },
  {
    id: 2,
    created_at: '2024-01-02T10:00:00Z',
    started_at: '2024-01-02T10:00:00Z',
    completed_at: '2024-01-02T11:00:00Z',
    print_name: 'Pricey filament, short run',
    status: 'completed',
    printer_id: 1,
    filament_type: 'PLA',
    filament_used_grams: 80,
    actual_time_seconds: 3600,
    print_time_seconds: 3600,
    cost: 2.0,
    energy_cost: null,
    depreciation_cost: null,
    quantity: 1,
  },
];

function mockStats(stats: Record<string, unknown>) {
  server.use(
    http.get('/api/v1/archives/stats', () => HttpResponse.json(stats)),
    http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'X1 Carbon', model: 'X1C' }])),
    http.get('/api/v1/archives/slim', () => HttpResponse.json(runs)),
    http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'USD' })),
    http.get('/api/v1/archives/analysis/failures', () =>
      HttpResponse.json({
        period_days: 30,
        total_prints: 2,
        failed_prints: 0,
        failure_rate: 0,
        failures_by_reason: {},
        failures_by_filament: {},
        failures_by_printer: {},
        failures_by_hour: {},
        recent_failures: [],
        trend: [],
      }),
    ),
  );
}

describe('StatsPage printer wear', () => {
  beforeEach(() => {
    mockStats(baseStats);
  });

  it('hides the Wear & Tear tile while nothing has accrued', async () => {
    render(<StatsPage />);
    await waitFor(() => expect(screen.getByText('Filament Cost')).toBeInTheDocument());

    expect(screen.queryByText('Wear & Tear')).not.toBeInTheDocument();
  });

  it('shows the Wear & Tear tile once the total is positive', async () => {
    mockStats({ ...baseStats, total_depreciation_cost: 1.5 });
    render(<StatsPage />);

    await waitFor(() => expect(screen.getByText('Wear & Tear')).toBeInTheDocument());
    expect(screen.getByText('$ 1.50')).toBeInTheDocument();
  });

  it('counts depreciation towards the costliest print', async () => {
    render(<StatsPage />);
    await waitFor(() => expect(screen.getByText('Most Expensive')).toBeInTheDocument());

    // 1.0 filament + 1.5 wear beats 2.0 filament alone.
    const record = screen.getByText('Most Expensive').parentElement!;
    expect(record).toHaveTextContent('$2.50');
    expect(record).toHaveTextContent('Cheap filament, long run');
  });
});
