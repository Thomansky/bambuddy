/**
 * Printer wear on the Archives page (#694).
 *
 * The card shows a third small cost figure — next to filament and energy —
 * only for archives that carry a depreciation snapshot, and the Print Log
 * offers the per-run value as an opt-in column like cost / energy cost.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { ArchivesPage } from '../../pages/ArchivesPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const baseArchive = {
  filename: 'benchy.gcode.3mf',
  printer_id: 1,
  printer_name: 'X1 Carbon',
  print_time_seconds: 3600,
  filament_used_grams: 15.5,
  status: 'completed',
  started_at: '2024-01-01T10:00:00Z',
  completed_at: '2024-01-01T12:30:00Z',
  actual_time_seconds: 9000,
  thumbnail_path: null,
  notes: null,
  project_id: null,
  project_name: null,
  tags: '',
  created_at: '2024-01-01T09:00:00Z',
  updated_at: '2024-01-01T12:30:00Z',
};

const mockArchives = [
  { ...baseArchive, id: 1, print_name: 'Worn Benchy', cost: 0.42, energy_cost: null, depreciation_cost: 0.5 },
  { ...baseArchive, id: 2, print_name: 'Free Benchy', cost: 0.42, energy_cost: null, depreciation_cost: null },
];

describe('ArchivesPage printer wear', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/archives/', () => HttpResponse.json(mockArchives)),
      http.get('/api/v1/archives/stats', () => HttpResponse.json({})),
      http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'X1 Carbon' }])),
      http.get('/api/v1/projects/', () => HttpResponse.json([])),
      http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
      http.get('/api/v1/archives/:id/plates', ({ params }) =>
        HttpResponse.json({ archive_id: Number(params.id), filename: 'x.3mf', plates: [], is_multi_plate: false }),
      ),
      http.get('/api/v1/archives/:id/filament-requirements', () => HttpResponse.json([])),
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'USD' })),
      http.get('/api/v1/print-log/', () => HttpResponse.json({ items: [], total: 0 })),
    );
  });

  it('renders the wear figure only on the card that has a snapshot', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(screen.getByText('Worn Benchy')).toBeInTheDocument());

    // Filament cost on both cards, wear on one.
    expect(screen.getAllByText('$0.42')).toHaveLength(2);
    expect(screen.getAllByText('$0.50')).toHaveLength(1);
    expect(document.querySelectorAll('.lucide-hourglass')).toHaveLength(1);
  });

  it('explains the figure as hours × rate in the tooltip', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(screen.getByText('Worn Benchy')).toBeInTheDocument());

    // 9000 s = 2.5 h; 0.50 / 2.5 h = 0.20/h — the rate the print was charged at.
    const figure = screen.getByText('$0.50').closest('div');
    expect(figure).toHaveAttribute('title', 'Printer wear: 2.5 h × 0.20 $/h');
  });

  it('offers a hidden-by-default Printer Wear column in the print log', async () => {
    server.use(
      http.get('/api/v1/print-log/', () =>
        HttpResponse.json({
          items: [
            {
              id: 1,
              archive_id: 1,
              print_name: 'Worn Benchy',
              printer_name: 'X1 Carbon',
              printer_id: 1,
              status: 'completed',
              started_at: '2024-01-01T10:00:00Z',
              completed_at: '2024-01-01T12:30:00Z',
              duration_seconds: 9000,
              filament_type: 'PLA',
              filament_color: '#000000',
              filament_used_grams: 15.5,
              cost: 0.42,
              energy_kwh: null,
              energy_cost: null,
              depreciation_cost: 0.5,
              failure_reason: null,
              thumbnail_path: null,
              created_by_id: null,
              created_by_username: null,
              created_at: '2024-01-01T10:00:00Z',
            },
          ],
          total: 1,
        }),
      ),
    );
    render(<ArchivesPage />);
    await waitFor(() => expect(screen.getByTitle('Print Log')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Print Log'));
    await waitFor(() => expect(screen.getByText('All Statuses')).toBeInTheDocument());

    // Off by default, like cost / energy: the table stays narrow for the
    // farms that never set a wear rate.
    const table = await screen.findByRole('table');
    expect(within(table).queryByText('Printer Wear')).not.toBeInTheDocument();
    expect(within(table).queryByText('$0.50')).not.toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: /Columns/ }));
    expect(await screen.findByText('Configure Columns')).toBeInTheDocument();
    const wearRow = screen.getByText('Printer Wear').closest('div')!;
    await user.click(within(wearRow).getByTitle('Show column'));
    await user.click(screen.getByRole('button', { name: 'Apply Changes' }));

    expect(await within(table).findByText('Printer Wear')).toBeInTheDocument();
    expect(within(table).getByText('$0.50')).toBeInTheDocument();
  });
});
