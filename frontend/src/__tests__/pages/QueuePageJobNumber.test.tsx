/**
 * The running number on the print queue.
 *
 * A number nobody can see, and nobody can look a job up by, is decoration —
 * so these cover both halves: the badge on the row, and the search box finding
 * the row by the number someone read out over the phone.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { QueuePage } from '../../pages/QueuePage';

const BASE_ITEM = {
  printer_id: 1,
  position: 1,
  status: 'pending',
  scheduled_time: null,
  require_previous_success: false,
  auto_off_after: false,
  manual_start: false,
  ams_mapping: null,
  plate_id: null,
  bed_levelling: 'on',
  flow_cali: 'off',
  vibration_cali: true,
  layer_inspect: false,
  timelapse: false,
  use_ams: true,
  started_at: null,
  completed_at: null,
  error_message: null,
  created_at: '2026-01-01T00:00:00Z',
  archive_thumbnail: null,
  printer_name: 'Test Printer',
  print_time_seconds: 3600,
};

const QUEUE_ITEMS = [
  { ...BASE_ITEM, id: 1, archive_id: 1, position: 1, job_number: 'A-01125', archive_name: 'Bracket' },
  { ...BASE_ITEM, id: 2, archive_id: 2, position: 2, job_number: 'A-01126', archive_name: 'Spacer' },
  // An item queued before the series was switched on: no number, still listed.
  { ...BASE_ITEM, id: 3, archive_id: 3, position: 3, job_number: null, archive_name: 'Older job' },
];

// Three numbered rows where a search can hide the first one, so a reorder of
// what's left has something to collide with.
const SEARCHABLE_QUEUE = [
  { ...BASE_ITEM, id: 1, archive_id: 1, position: 1, job_number: 'B-01125', archive_name: 'Bracket' },
  { ...BASE_ITEM, id: 2, archive_id: 2, position: 2, job_number: 'A-01126', archive_name: 'Spacer' },
  { ...BASE_ITEM, id: 3, archive_id: 3, position: 3, job_number: 'A-01127', archive_name: 'Flange' },
];

const PRINTERS = [
  {
    id: 1,
    name: 'Test Printer',
    ip_address: '192.168.1.100',
    serial_number: 'TESTSERIAL0001',
    access_code: '12345678',
    model: 'X1C',
    enabled: true,
    created_at: '2026-01-01T00:00:00Z',
  },
];

describe('Print queue job numbers', () => {
  beforeEach(() => {
    vi.mocked(localStorage.getItem).mockImplementation((key: string) => {
      if (key === 'queue.viewMode') return 'list';
      return null;
    });
    server.use(
      http.get('/api/v1/queue/', () => HttpResponse.json(QUEUE_ITEMS)),
      http.get('/api/v1/printers/', () => HttpResponse.json(PRINTERS)),
    );
  });

  it('shows the job number next to the name', async () => {
    render(<QueuePage />);

    await waitFor(() => expect(screen.getByText('Bracket')).toBeInTheDocument());
    expect(screen.getByText('A-01125')).toBeInTheDocument();
  });

  it('leaves a job without a number unlabelled rather than blank-badged', async () => {
    render(<QueuePage />);

    await waitFor(() => expect(screen.getByText('Older job')).toBeInTheDocument());
    expect(screen.queryByText('null')).not.toBeInTheDocument();
  });

  it('finds a job by its number', async () => {
    render(<QueuePage />);
    await waitFor(() => expect(screen.getByText('Bracket')).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText('Search by name or number'), 'A-01126');

    await waitFor(() => expect(screen.queryByText('Bracket')).not.toBeInTheDocument());
    expect(screen.getByText('Spacer')).toBeInTheDocument();
    expect(screen.queryByText('Older job')).not.toBeInTheDocument();
  });

  it('still finds a job by its name', async () => {
    render(<QueuePage />);
    await waitFor(() => expect(screen.getByText('Bracket')).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText('Search by name or number'), 'Older');

    await waitFor(() => expect(screen.queryByText('Bracket')).not.toBeInTheDocument());
    expect(screen.getByText('Older job')).toBeInTheDocument();
  });

  // Looking a job up by its number and then nudging it up the queue is the
  // workflow the search box exists for, and a reorder renumbers positions —
  // which the rows the search is hiding still hold.
  it('renumbers the rows the search is hiding as well', async () => {
    let reorderBody: { items: { id: number; position: number }[] } | null = null;
    server.use(
      http.get('/api/v1/queue/', () => HttpResponse.json(SEARCHABLE_QUEUE)),
      http.post('/api/v1/queue/reorder', async ({ request }) => {
        reorderBody = (await request.json()) as typeof reorderBody;
        return HttpResponse.json({ message: 'ok' });
      }),
    );
    render(<QueuePage />);
    await waitFor(() => expect(screen.getByText('Bracket')).toBeInTheDocument());

    await userEvent.type(screen.getByLabelText('Search by name or number'), 'A-0112');
    await waitFor(() => expect(screen.queryByText('Bracket')).not.toBeInTheDocument());

    // Spacer (position 2) drops below Flange (position 3).
    await userEvent.click(screen.getAllByTitle('Move Up')[1]);

    await waitFor(() => expect(reorderBody).not.toBeNull());
    // Bracket is out of sight but still holds position 1; sending 1 and 2 for
    // the two visible rows would have handed its number away.
    expect(reorderBody!.items).toEqual([
      { id: 1, position: 1 },
      { id: 3, position: 2 },
      { id: 2, position: 3 },
    ]);
  });
});
