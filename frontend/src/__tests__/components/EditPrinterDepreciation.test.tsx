/**
 * Printer wear rate on the Edit Printer dialog (#694).
 *
 * One optional field — wear cost per printing hour — shows the configured
 * currency and travels with the PATCH so the backend can price future prints
 * from it. Empty means "off" and is sent as null.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { depreciationFieldToApi, depreciationFieldFromApi } from '../../utils/depreciation';

const mockPrinter = {
  id: 1,
  name: 'X1 Carbon',
  ip_address: '192.168.1.100',
  serial_number: '00M09A350100001',
  access_code: '12345678',
  model: 'X1C',
  enabled: true,
  location: null,
  auto_archive: true,
  is_active: true,
  wear_cost_per_hour: 0.5,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const mockStatus = {
  connected: true,
  state: 'IDLE',
  progress: 0,
  layer_num: 0,
  total_layers: 0,
  temperatures: { nozzle: 25, bed: 25, chamber: 25 },
  remaining_time: 0,
  filename: null,
  wifi_signal: -50,
  vt_tray: [],
};

const RATE_LABEL = 'Wear cost per printing hour (optional)';

async function openEditModal() {
  render(<PrintersPage />);
  await waitFor(() => expect(screen.getByText('X1 Carbon')).toBeInTheDocument());
  const menuBtn = [...document.querySelectorAll('button')].find((b) =>
    b.querySelector('.lucide-ellipsis-vertical'),
  )!;
  await userEvent.click(menuBtn);
  await userEvent.click(await screen.findByRole('button', { name: /^edit$/i }));
  await screen.findByText('Edit Printer');
}

describe('form helpers', () => {
  it('maps empty fields to null and numbers through unchanged', () => {
    expect(depreciationFieldToApi('')).toBeNull();
    expect(depreciationFieldToApi('   ')).toBeNull();
    expect(depreciationFieldToApi('0.5')).toBe(0.5);
    expect(depreciationFieldToApi('0')).toBe(0);
    expect(depreciationFieldToApi('abc')).toBeNull();
    expect(depreciationFieldFromApi(null)).toBe('');
    expect(depreciationFieldFromApi(undefined)).toBe('');
    expect(depreciationFieldFromApi(0.5)).toBe('0.5');
  });
});

describe('EditPrinterModal wear rate field', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockStatus)),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
    );
  });

  it('shows the stored rate, the currency symbol and the help text', async () => {
    await openEditModal();

    const rate = screen.getByLabelText(RATE_LABEL) as HTMLInputElement;
    expect(rate.value).toBe('0.5');
    expect(rate.step).toBe('0.0001');
    // Currency adornment on the input, from the configured setting.
    expect(screen.getByText('€')).toBeInTheDocument();
    expect(screen.getByText(/depreciation, maintenance, parts/)).toBeInTheDocument();
    // The old two-input form and its derived-rate line are gone.
    expect(screen.queryByLabelText(/purchase price/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/expected lifetime/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/≈ .* printer wear/)).not.toBeInTheDocument();
  });

  it('sends the typed rate on save', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({ printer_id: null, ip_address: '192.168.1.100', overall: 'ok', checks: [] }),
      ),
      http.patch('/api/v1/printers/:id', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...mockPrinter, ...body });
      }),
    );

    await openEditModal();
    const rate = screen.getByLabelText(RATE_LABEL);
    await userEvent.clear(rate);
    await userEvent.type(rate, '0.75');
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.wear_cost_per_hour).toBe(0.75);
    expect(body).not.toHaveProperty('purchase_price');
    expect(body).not.toHaveProperty('expected_lifetime_hours');
  });

  it('sends null when the field is cleared', async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      http.post('/api/v1/printers/diagnostic', () =>
        HttpResponse.json({ printer_id: null, ip_address: '192.168.1.100', overall: 'ok', checks: [] }),
      ),
      http.patch('/api/v1/printers/:id', async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...mockPrinter, ...body });
      }),
    );

    await openEditModal();
    await userEvent.clear(screen.getByLabelText(RATE_LABEL));
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.wear_cost_per_hour).toBeNull();
  });
});
