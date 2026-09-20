/**
 * The Finance page labels its Amount column with the VAT working basis for
 * the readers it exists for: a cost_centers:read_own user who cannot fetch
 * /settings (403) and reads /settings/ui-flags instead (#3023).
 */

import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { FinancePage } from '../../pages/FinancePage';
import { server } from '../mocks/server';

const transaction = {
  id: 1,
  user_id: 1,
  cost_center_id: null,
  transaction_type: 'print_charge',
  amount: -4.2,
  balance_after: 8.14,
  description: 'Benchy',
  created_by_user_id: null,
  print_run_id: null,
  print_archive_id: null,
  print_queue_id: null,
  created_at: '2026-09-20T10:00:00Z',
};

function mockFinance(uiFlags: Record<string, unknown>) {
  server.use(
    http.get('*/api/v1/settings/', () => HttpResponse.json({ detail: 'Permission denied' }, { status: 403 })),
    http.get('*/api/v1/settings/ui-flags', () => HttpResponse.json({ billing_enabled: true, currency: 'EUR', ...uiFlags })),
    http.get('*/api/v1/finance/me/balance', () =>
      HttpResponse.json({ user_id: 1, balance: 8.14, currency: 'EUR', updated_at: null }),
    ),
    http.get('*/api/v1/finance/me/transactions', () => HttpResponse.json({ items: [transaction], total: 1 })),
    http.get('*/api/v1/finance/transactions', () => HttpResponse.json({ items: [transaction], total: 1 })),
    http.get('*/api/v1/finance/cost-centers/mine', () => HttpResponse.json([])),
    http.get('*/api/v1/finance/cost-centers', () => HttpResponse.json([])),
    http.get('*/api/v1/users/slim', () => HttpResponse.json([])),
  );
}

describe('FinancePage VAT working basis', () => {
  it('labels the Amount column for a reader who cannot fetch /settings', async () => {
    mockFinance({ vat_enabled: true, price_vat_basis: 'gross' });

    render(<FinancePage />);

    await waitFor(() => expect(screen.getByText('-€4.20')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('incl. VAT')).toBeInTheDocument());
  });

  it('shows a plain Amount header while the VAT distinction is off', async () => {
    mockFinance({ vat_enabled: false });

    render(<FinancePage />);

    await waitFor(() => expect(screen.getByText('-€4.20')).toBeInTheDocument());
    expect(screen.queryByText(/VAT/)).not.toBeInTheDocument();
  });
});
