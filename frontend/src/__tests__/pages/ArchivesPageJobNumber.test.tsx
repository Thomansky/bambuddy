/**
 * The job number in Print History.
 *
 * The queue row is deleted once the order is done, so the archive is where the
 * number has to be readable afterwards — on the card, as an optional Print Log
 * column, and through the search box a farm actually uses to find a job.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ArchivesPage } from '../../pages/ArchivesPage';

const BASE_ARCHIVE = {
  filename: 'part.gcode.3mf',
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

const ARCHIVES = [
  { ...BASE_ARCHIVE, id: 10, print_name: 'Bracket', job_number: 'A-01125' },
  { ...BASE_ARCHIVE, id: 11, print_name: 'Spacer', job_number: 'A-01126' },
  // Printed before the series existed — no number, still listed.
  { ...BASE_ARCHIVE, id: 12, print_name: 'Older print', job_number: null },
];

const LOG_ENTRIES = [
  {
    id: 1,
    archive_id: 10,
    job_number: 'A-01125',
    print_name: 'Bracket',
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
    failure_reason: null,
    thumbnail_path: null,
    created_by_id: null,
    created_by_username: null,
    created_at: '2026-07-24T18:35:00Z',
  },
];

function stubStoredColumns(value: string | null) {
  vi.mocked(localStorage.getItem).mockImplementation((key: string) =>
    key === 'bambuddy-printlog-columns' ? value : null,
  );
}

beforeEach(() => {
  stubStoredColumns(null);
  server.use(
    http.get('/api/v1/archives/', () => HttpResponse.json(ARCHIVES)),
    http.get('/api/v1/archives/stats', () =>
      HttpResponse.json({
        total_archives: ARCHIVES.length,
        total_print_time_seconds: 0,
        total_filament_grams: 0,
        prints_this_week: 0,
        prints_this_month: 0,
      }),
    ),
    http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
    http.get('/api/v1/print-log/', () =>
      HttpResponse.json({ items: LOG_ENTRIES, total: LOG_ENTRIES.length }),
    ),
  );
});

describe('Job numbers in Print History', () => {
  it('shows the job number on the card', async () => {
    render(<ArchivesPage />);

    await waitFor(() => expect(screen.getByText('Bracket')).toBeInTheDocument());
    expect(screen.getByText('A-01125')).toBeInTheDocument();
  });

  it('finds a print by its job number', async () => {
    render(<ArchivesPage />);
    await waitFor(() => expect(screen.getByText('Bracket')).toBeInTheDocument());

    await userEvent.type(screen.getByPlaceholderText('Search archives...'), 'A-01126');

    await waitFor(() => expect(screen.queryByText('Bracket')).not.toBeInTheDocument());
    expect(screen.getByText('Spacer')).toBeInTheDocument();
    expect(screen.queryByText('Older print')).not.toBeInTheDocument();
  });
});

describe('The Print Log job-number column', () => {
  async function openLogView() {
    render(<ArchivesPage />);
    await waitFor(() => expect(screen.getByTitle('Print Log')).toBeInTheDocument());
    fireEvent.click(screen.getByTitle('Print Log'));
    await waitFor(() => expect(screen.getByText('All Statuses')).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByText('No print log entries found')).toBeNull());
    return screen.findByRole('table');
  }

  it('ships hidden, like the other optional columns', async () => {
    const table = await openLogView();

    expect(within(table).queryByText('Job No.')).not.toBeInTheDocument();
  });

  it('renders the number once switched on', async () => {
    stubStoredColumns(
      JSON.stringify([
        { id: 'print_name', visible: true },
        { id: 'job_number', visible: true },
      ]),
    );
    const table = await openLogView();

    expect(within(table).getByText('Job No.')).toBeInTheDocument();
    expect(within(table).getByText('A-01125')).toBeInTheDocument();
  });
});
