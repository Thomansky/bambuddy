/**
 * The Settings → Workflow card that runs the number series.
 *
 * Two things matter here. One, the card has to round-trip a series through the
 * API — switching it on and changing its shape are the whole feature's front
 * door. Two, the "next: …" preview has to agree with what the backend will
 * actually store: it is a promise about the next number, and a preview that
 * disagrees would send someone to look for a quote that does not exist.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, fireEvent, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { api, renderSeriesNumber, type NumberSeries } from '../../api/client';
import { NumberSeriesSettings } from '../../components/NumberSeriesSettings';

vi.mock('../../api/client', async () => {
  const actual: typeof import('../../api/client') = await vi.importActual('../../api/client');
  return {
    ...actual,
    api: {
      ...actual.api,
      getNumberSeries: vi.fn(),
      updateNumberSeries: vi.fn(),
    },
    getAuthToken: vi.fn(() => null),
  };
});

function series(overrides: Partial<NumberSeries> = {}): NumberSeries {
  const base: NumberSeries = {
    key: 'project',
    enabled: false,
    prefix: '',
    suffix: '',
    next_value: 1,
    padding: 0,
    updated_at: null,
    preview: '1',
  };
  const merged = { ...base, ...overrides };
  return {
    ...merged,
    preview: renderSeriesNumber(merged.prefix, merged.next_value, merged.padding, merged.suffix),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('NumberSeriesSettings', () => {
  it('switches a series on through the API', async () => {
    vi.mocked(api.getNumberSeries).mockResolvedValue([series()]);
    vi.mocked(api.updateNumberSeries).mockResolvedValue(series({ enabled: true }));

    render(<NumberSeriesSettings />);
    const toggle = await screen.findByRole('checkbox', { name: /Projects/ });

    fireEvent.click(toggle);

    await waitFor(() =>
      expect(api.updateNumberSeries).toHaveBeenCalledWith('project', { enabled: true }),
    );
  });

  it('sends the prefix, the next number and the padding the user typed', async () => {
    vi.mocked(api.getNumberSeries).mockResolvedValue([series({ enabled: true })]);
    vi.mocked(api.updateNumberSeries).mockResolvedValue(
      series({ enabled: true, prefix: 'A-', next_value: 1125, padding: 5 }),
    );

    render(<NumberSeriesSettings />);
    const prefix = await screen.findByLabelText('Prefix');

    fireEvent.change(prefix, { target: { value: 'A-' } });
    fireEvent.blur(prefix);
    await waitFor(() => expect(api.updateNumberSeries).toHaveBeenCalledWith('project', { prefix: 'A-' }));

    const next = screen.getByLabelText('Next number');
    fireEvent.change(next, { target: { value: '1125' } });
    fireEvent.blur(next);
    await waitFor(() => expect(api.updateNumberSeries).toHaveBeenCalledWith('project', { next_value: 1125 }));

    const padding = screen.getByLabelText('Digits');
    fireEvent.change(padding, { target: { value: '5' } });
    fireEvent.blur(padding);
    await waitFor(() => expect(api.updateNumberSeries).toHaveBeenCalledWith('project', { padding: 5 }));
  });

  it('previews exactly what the backend says the next number will be', async () => {
    // `preview` on the row is the backend's own render of these settings; the
    // card computes its own while the user types, and the two must match.
    const stored = series({ enabled: true, prefix: 'A-', suffix: '/26', next_value: 1125, padding: 5 });
    expect(stored.preview).toBe('A-01125/26');
    vi.mocked(api.getNumberSeries).mockResolvedValue([stored]);

    render(<NumberSeriesSettings />);

    expect(await screen.findByText(`next: ${stored.preview}`)).toBeInTheDocument();
  });

  it('follows the field being typed rather than the saved value', async () => {
    vi.mocked(api.getNumberSeries).mockResolvedValue([series({ enabled: true, padding: 4 })]);

    render(<NumberSeriesSettings />);
    const next = await screen.findByLabelText('Next number');

    fireEvent.change(next, { target: { value: '42' } });

    expect(await screen.findByText('next: 0042')).toBeInTheDocument();
  });

  it('warns when the next number is moved backwards', async () => {
    vi.mocked(api.getNumberSeries).mockResolvedValue([series({ enabled: true, next_value: 500 })]);

    render(<NumberSeriesSettings />);
    const next = await screen.findByLabelText('Next number');
    expect(screen.queryByText(/already been used/)).not.toBeInTheDocument();

    fireEvent.change(next, { target: { value: '10' } });

    expect(await screen.findByText(/already been used/)).toBeInTheDocument();
  });

  it('keeps each series on its own row', async () => {
    vi.mocked(api.getNumberSeries).mockResolvedValue([
      series({ key: 'project', prefix: 'P-' }),
      series({ key: 'queue_job', prefix: 'J-' }),
    ]);

    render(<NumberSeriesSettings />);

    expect(await screen.findByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('Queue jobs')).toBeInTheDocument();
    expect(screen.getAllByLabelText('Prefix')).toHaveLength(2);
  });
});
