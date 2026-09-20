/**
 * Tests for the FilamentTrends widget's print counting.
 *
 * An archive edited down to 0 items produced nothing (#3051). The count here
 * used to read `quantity || 1`, which turned that 0 back into one print and
 * left this widget contradicting the project page.
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { FilamentTrends } from '../../components/FilamentTrends';
import type { ArchiveSlim } from '../../api/client';

const archive = (overrides: Partial<ArchiveSlim>): ArchiveSlim => ({
  printer_id: 1,
  print_name: 'Benchy',
  print_time_seconds: 3600,
  actual_time_seconds: 3600,
  filament_used_grams: 20,
  filament_type: 'PLA',
  filament_color: '#00ae42',
  status: 'completed',
  started_at: '2026-09-06T10:00:00Z',
  completed_at: '2026-09-06T11:00:00Z',
  cost: 1,
  energy_kwh: 0.1,
  energy_cost: 0.02,
  quantity: 1,
  created_at: '2026-09-06T10:00:00Z',
  ...overrides,
});

/** The "<n> prints" caption under the summary heading. */
const printCount = () =>
  screen
    .getAllByText((_, el) => el?.tagName === 'P' && /^\d+ prints$/.test(el.textContent ?? ''))
    .map((el) => el.textContent)[0];

describe('FilamentTrends print count (#3051)', () => {
  it('counts a ruined plate as zero prints, not one', () => {
    render(<FilamentTrends archives={[archive({ quantity: 0 }), archive({ quantity: 3 })]} />);

    expect(printCount()).toBe('3 prints');
  });

  it('still treats a missing quantity as a single print', () => {
    const noQuantity = archive({});
    delete (noQuantity as Partial<ArchiveSlim>).quantity;

    render(<FilamentTrends archives={[noQuantity as ArchiveSlim]} />);

    expect(printCount()).toBe('1 prints');
  });
});

describe('FilamentTrends VAT working basis', () => {
  it('labels the period cost, the average and the energy cost once VAT is on', async () => {
    server.use(
      http.get('/api/v1/settings/ui-flags', () =>
        HttpResponse.json({ currency: 'EUR', vat_enabled: true, price_vat_basis: 'net' })
      )
    );

    render(<FilamentTrends archives={[archive({ cost: 2.5, energy_kwh: 0.2, energy_cost: 0.04 })]} currency="€" />);

    await waitFor(() => {
      expect(screen.getAllByText('excl. VAT')).toHaveLength(3);
    });
  });

  it('shows plain amounts while the VAT distinction is off', async () => {
    render(<FilamentTrends archives={[archive({})]} currency="€" />);

    expect(screen.getAllByText(/€1\.00/).length).toBeGreaterThan(0);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByText(/VAT/)).not.toBeInTheDocument();
  });
});
