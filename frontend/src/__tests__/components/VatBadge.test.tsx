import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { VatBadge } from '../../components/VatBadge';

// The badge must read /settings/ui-flags, which any signed-in user can fetch,
// and never /settings, which needs settings:read (#3023). Every case here
// answers /settings with the 403 a non-admin gets, so a badge that went back
// to /settings would show no label while VAT is on.
const uiFlags = (overrides: Record<string, unknown>) =>
  http.get('/api/v1/settings/ui-flags', () => HttpResponse.json({ currency: 'EUR', ...overrides }));
const settingsForbidden = http.get('/api/v1/settings/', () =>
  HttpResponse.json({ detail: 'Permission denied' }, { status: 403 })
);

describe('VatBadge', () => {
  beforeEach(() => {
    server.use(settingsForbidden, uiFlags({ vat_enabled: true, price_vat_basis: 'gross' }));
  });

  it('shows the gross suffix after an amount', async () => {
    render(<span>12.40 €<VatBadge /></span>);
    await waitFor(() => expect(screen.getByText('incl. VAT')).toBeInTheDocument());
  });

  it('shows the net suffix when the working basis is net', async () => {
    server.use(uiFlags({ vat_enabled: true, price_vat_basis: 'net' }));
    render(<VatBadge />);
    await waitFor(() => expect(screen.getByText('excl. VAT')).toBeInTheDocument());
  });

  it('renders nothing while the VAT distinction is off', async () => {
    server.use(uiFlags({ vat_enabled: false, price_vat_basis: 'net' }));
    const { container } = render(<VatBadge />);
    // Give the flags query a chance to resolve before asserting absence.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(container.textContent).toBe('');
    expect(screen.queryByText(/VAT/)).not.toBeInTheDocument();
  });
});
