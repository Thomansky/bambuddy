/**
 * Print Log wear column across the depreciation_cost -> wear_cost rename.
 *
 * The fork stored the printer wear column under the id `depreciation_cost`
 * before the merge with upstream renamed it to `wear_cost`. The backend
 * migration carries the values over, but the per-browser column and sort
 * settings still name the old id. Without mapping it on load, the loaders
 * drop it as unknown: the wear column a user had switched on comes back
 * hidden and a sort on it falls back to date.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ArchivesPage } from '../../pages/ArchivesPage';

const LOG_ENTRY = {
  id: 1,
  archive_id: 10,
  print_name: 'Califlower Calibration',
  printer_name: '3DP-00M-191',
  printer_id: 1,
  status: 'completed',
  started_at: '2026-07-24T18:35:00Z',
  completed_at: '2026-07-24T19:24:00Z',
  duration_seconds: 2940,
  filament_type: 'PLA',
  filament_color: '#000000',
  filament_used_grams: 15.5,
  cost: 0.42,
  energy_kwh: 0.31,
  energy_cost: 0.09,
  wear_cost: 0.25,
  failure_reason: null,
  thumbnail_path: null,
  created_by_id: null,
  created_by_username: null,
  created_at: '2026-07-24T18:35:00Z',
};

const ONE_ARCHIVE = {
  id: 10,
  filename: 'cali.gcode.3mf',
  print_name: 'Califlower',
  printer_id: 1,
  printer_name: '3DP-00M-191',
  print_time_seconds: 2940,
  filament_used_grams: 15.5,
  status: 'completed',
  started_at: '2026-07-24T18:35:00Z',
  completed_at: '2026-07-24T19:24:00Z',
  thumbnail_path: null,
  notes: null,
  rating: null,
  project_id: null,
  project_name: null,
  project_color: null,
  print_count: 1,
  tags: '',
  created_at: '2026-07-24T18:00:00Z',
  updated_at: '2026-07-24T19:24:00Z',
  has_f3d: false,
};

/** Query strings the page asked the log endpoint for, newest last. */
const logRequests: URLSearchParams[] = [];

function mockLog() {
  server.use(
    http.get('/api/v1/archives/', () => HttpResponse.json([ONE_ARCHIVE])),
    http.get('/api/v1/archives/stats', () =>
      HttpResponse.json({
        total_archives: 0,
        total_print_time_seconds: 0,
        total_filament_grams: 0,
        prints_this_week: 0,
        prints_this_month: 0,
      }),
    ),
    http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
    http.get('/api/v1/print-log/', ({ request }) => {
      logRequests.push(new URL(request.url).searchParams);
      return HttpResponse.json({ items: [LOG_ENTRY], total: 1 });
    }),
  );
}

/** `setup.ts` replaces localStorage with a no-op vi.fn() stub, so stored
 *  settings are handed to the component through the mock. Every other key
 *  answers null so view mode, page size and the rest keep their defaults. */
function stubStorage(values: { columns?: unknown; sort?: unknown }) {
  vi.mocked(localStorage.getItem).mockImplementation((key: string) => {
    if (key === 'bambuddy-printlog-columns' && values.columns !== undefined) {
      return JSON.stringify(values.columns);
    }
    if (key === 'bambuddy-printlog-sort' && values.sort !== undefined) {
      return JSON.stringify(values.sort);
    }
    return null;
  });
}

async function openLogView() {
  render(<ArchivesPage />);
  await waitFor(() => expect(screen.getByTitle('Print Log')).toBeInTheDocument());
  fireEvent.click(screen.getByTitle('Print Log'));
  await waitFor(() => expect(screen.getByText('All Statuses')).toBeInTheDocument());
  await waitFor(() => expect(screen.queryByText('No print log entries found')).toBeNull());
}

describe('Print Log wear column migration', () => {
  beforeEach(() => {
    logRequests.length = 0;
    stubStorage({});
    mockLog();
  });

  it('keeps a wear column and sort stored under the old depreciation_cost id', async () => {
    stubStorage({
      columns: [
        { id: 'date', visible: true },
        { id: 'depreciation_cost', visible: true },
      ],
      sort: { column: 'depreciation_cost', direction: 'asc' },
    });
    await openLogView();

    const table = screen.getByRole('table');
    expect(within(table).getByText('Wear Cost')).toBeInTheDocument();
    expect(within(table).getByText('$0.25')).toBeInTheDocument();

    // The sort reaches the API under the new id instead of resetting to date.
    expect(logRequests[0].get('sort_by')).toBe('wear_cost');
    expect(logRequests[0].get('sort_dir')).toBe('asc');
    expect(screen.getByRole('columnheader', { name: /Wear Cost/ })).toHaveAttribute(
      'aria-sort',
      'ascending',
    );
  });

  it('renders the wear column once when both the old and the new id are stored', async () => {
    // The first entry wins: here the legacy one, which the user switched on.
    stubStorage({
      columns: [
        { id: 'date', visible: true },
        { id: 'depreciation_cost', visible: true },
        { id: 'wear_cost', visible: false },
      ],
    });
    await openLogView();

    const table = screen.getByRole('table');
    expect(within(table).getAllByText('Wear Cost')).toHaveLength(1);
    expect(within(table).getByText('$0.25')).toBeInTheDocument();
  });
});
