import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { VatBadge } from '../../components/VatBadge';

const settings = (overrides: Record<string, unknown>) =>
  http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR', ...overrides }));

describe('VatBadge', () => {
  beforeEach(() => {
    server.use(settings({ vat_enabled: true, price_vat_basis: 'gross' }));
  });

  it('shows the gross suffix after an amount', async () => {
    render(<span>12.40 €<VatBadge /></span>);
    await waitFor(() => expect(screen.getByText('incl. VAT')).toBeInTheDocument());
  });

  it('shows the net suffix when the working basis is net', async () => {
    server.use(settings({ vat_enabled: true, price_vat_basis: 'net' }));
    render(<VatBadge />);
    await waitFor(() => expect(screen.getByText('excl. VAT')).toBeInTheDocument());
  });

  it('renders nothing while the VAT distinction is off', async () => {
    server.use(settings({ vat_enabled: false, price_vat_basis: 'net' }));
    const { container } = render(<VatBadge />);
    // Give the settings query a chance to resolve before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container.textContent).toBe('');
    expect(screen.queryByText(/VAT/)).not.toBeInTheDocument();
  });
});
