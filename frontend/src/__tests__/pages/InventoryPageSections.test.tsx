/**
 * The filament stock page has three sections — spool inventory, stock
 * forecast, products (#3165). Table or cards is a view option of the spool
 * section, the spool stats belong to the spools only, and each section brings
 * its own header actions. The section is kept in the URL.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { server } from '../mocks/server';

function setupHandlers() {
  server.use(
    http.get('/api/v1/settings/spoolman', () =>
      HttpResponse.json({
        spoolman_enabled: 'false',
        spoolman_url: '',
        spoolman_sync_mode: 'auto',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'true',
      }),
    ),
    http.get('/api/v1/inventory/spools', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/products', () => HttpResponse.json([])),
    http.get('/api/v1/printers/', () => HttpResponse.json([])),
  );
}

function openAt(url: string) {
  window.history.replaceState({}, '', url);
}

describe('InventoryPage — sections', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
  });

  it('shows the spools with their stats, and table or cards as a view option', async () => {
    openAt('/inventory');
    render(<InventoryPageRouter />);

    expect(await screen.findByText('Total Inventory')).toBeInTheDocument();
    const view = screen.getByRole('group', { name: 'View' });
    expect(within(view).getByRole('button', { name: /Table/ })).toBeInTheDocument();
    expect(within(view).getByRole('button', { name: /Cards/ })).toBeInTheDocument();
    expect(within(view).queryByRole('button', { name: /Products|forecast/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Spool inventory/ })).toHaveAttribute('aria-current', 'page');
    expect(screen.getAllByRole('button', { name: /Add Spool/ }).length).toBeGreaterThan(0);
  });

  it('gives the products their own header actions and no spool stats', async () => {
    openAt('/inventory?section=products');
    render(<InventoryPageRouter />);

    expect(await screen.findByRole('button', { name: /New product/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Below target/ })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Take over spools/ }).length).toBeGreaterThan(0);
    expect(screen.queryByText('Total Inventory')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Add Spool/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'View' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Products/ })).toHaveAttribute('aria-current', 'page');
  });

  it('keeps the chosen section in the URL', async () => {
    const user = userEvent.setup();
    openAt('/inventory');
    render(<InventoryPageRouter />);
    await screen.findByText('Total Inventory');

    await user.click(screen.getByRole('button', { name: /^Products/ }));
    await waitFor(() => expect(window.location.search).toContain('section=products'));

    await user.click(screen.getByRole('button', { name: /Spool inventory/ }));
    await waitFor(() => expect(window.location.search).not.toContain('section='));
    expect(await screen.findByText('Total Inventory')).toBeInTheDocument();
  });
});
