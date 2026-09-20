/**
 * Printer depreciation inputs on the Edit Printer dialog (#694).
 *
 * The two optional fields (purchase price, expected lifetime hours) show the
 * configured currency, derive a live hourly wear rate, and travel with the
 * PATCH so the backend can price future prints from them.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import {
  deriveHourlyRate,
  depreciationFieldToApi,
  depreciationFieldFromApi,
} from '../../utils/depreciation';

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
  purchase_price: 1200,
  expected_lifetime_hours: 6000,
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
  it('derives the hourly rate only when both inputs are positive', () => {
    expect(deriveHourlyRate({ purchase_price: '1200', expected_lifetime_hours: '6000' })).toBeCloseTo(0.2);
    expect(deriveHourlyRate({ purchase_price: '', expected_lifetime_hours: '6000' })).toBeNull();
    expect(deriveHourlyRate({ purchase_price: '1200', expected_lifetime_hours: '' })).toBeNull();
    expect(deriveHourlyRate({ purchase_price: '1200', expected_lifetime_hours: '0' })).toBeNull();
    expect(deriveHourlyRate({ purchase_price: 'abc', expected_lifetime_hours: '10' })).toBeNull();
  });

  it('maps empty fields to null and numbers through unchanged', () => {
    expect(depreciationFieldToApi('')).toBeNull();
    expect(depreciationFieldToApi('   ')).toBeNull();
    expect(depreciationFieldToApi('1200.5')).toBe(1200.5);
    expect(depreciationFieldFromApi(null)).toBe('');
    expect(depreciationFieldFromApi(6000)).toBe('6000');
  });
});

describe('EditPrinterModal depreciation fields', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/printers/', () => HttpResponse.json([mockPrinter])),
      http.get('/api/v1/printers/:id/status', () => HttpResponse.json(mockStatus)),
      http.get('/api/v1/queue/', () => HttpResponse.json([])),
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
    );
  });

  it('shows the stored values, the currency symbol and the derived hourly rate', async () => {
    await openEditModal();

    const price = screen.getByLabelText('Purchase price (optional)') as HTMLInputElement;
    const hours = screen.getByLabelText('Expected lifetime (hours)') as HTMLInputElement;
    expect(price.value).toBe('1200');
    expect(hours.value).toBe('6000');
    expect(screen.getByText('≈ 0.20 €/h printer wear')).toBeInTheDocument();
    // Currency adornment on the price input, from the configured setting.
    expect(screen.getByText('€')).toBeInTheDocument();
  });

  it('recomputes the rate as the user types and shows the help text when incomplete', async () => {
    await openEditModal();

    const hours = screen.getByLabelText('Expected lifetime (hours)');
    await userEvent.clear(hours);
    await userEvent.type(hours, '3000');
    expect(screen.getByText('≈ 0.40 €/h printer wear')).toBeInTheDocument();

    await userEvent.clear(hours);
    expect(screen.queryByText(/printer wear$/)).not.toBeInTheDocument();
    expect(screen.getByText(/Leave both empty to disable/)).toBeInTheDocument();
  });

  it('sends both fields on save, and null when cleared', async () => {
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
    const price = screen.getByLabelText('Purchase price (optional)');
    await userEvent.clear(price);
    await userEvent.type(price, '900');
    await userEvent.clear(screen.getByLabelText('Expected lifetime (hours)'));
    await userEvent.click(screen.getByRole('button', { name: /save changes/i }));

    await waitFor(() => expect(body).not.toBeNull());
    expect(body!.purchase_price).toBe(900);
    expect(body!.expected_lifetime_hours).toBeNull();
  });
});
