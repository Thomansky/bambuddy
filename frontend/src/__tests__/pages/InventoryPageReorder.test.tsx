/**
 * The reorder list on the filament stock page: its own section with the
 * number of lines on its tab, and "Reorder" on every spool that belongs to a
 * product — in the table and on the cards.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { server } from '../mocks/server';

const baseSpool = {
  subtype: null,
  brand: 'eSun',
  color_name: 'Blue',
  rgba: '0000FFFF',
  extra_colors: null,
  effect_type: null,
  label_weight: 1000,
  core_weight: 250,
  core_weight_catalog_id: null,
  slicer_filament: null,
  slicer_filament_name: null,
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  added_full: null,
  last_used: null,
  encode_time: null,
  tag_uid: null,
  tray_uuid: null,
  data_origin: null,
  tag_type: null,
  archived_at: null,
  created_at: '2025-01-01T00:00:00Z',
  updated_at: '2025-01-01T00:00:00Z',
  k_profiles: [] as never[],
  cost_per_kg: null,
  last_scale_weight: null,
  last_weighed_at: null,
  storage_location: null,
  category: null,
  low_stock_threshold_pct: null,
  spoolman_id: null,
  spoolman_filament_id: null,
  weight_used: 400,
};

// One spool of a product, one that belongs to none.
const SPOOLS = [
  { ...baseSpool, id: 5, material: 'PETG', variant_id: 12 },
  { ...baseSpool, id: 6, material: 'ABS', color_name: 'Red', rgba: 'FF0000FF', variant_id: null },
];

const REORDER_LINE = {
  variant_id: 12,
  product_id: 1,
  product_label: 'eSun PETG',
  material_number: null,
  color_name: 'Blue',
  rgba: '0000FFFF',
  extra_colors: null,
  effect_type: null,
  label_weight: 1000,
  refill: false,
  min_stock: null,
  spools: 1,
  in_stock: 1,
  on_order: 0,
  shortfall: 0,
  list_price: null,
  price_vat_included: true,
  suppliers: [],
};

function orderLine(id: number, status: string) {
  return {
    ...REORDER_LINE,
    id,
    status,
    quantity: 1,
    reference: null,
    note: null,
    added_at: '2026-10-01T08:00:00',
    purchased_at: null,
    received_at: null,
    supplier_id: null,
    supplier_name: null,
    material: 'PETG',
    subtype: null,
    brand: 'eSun',
  };
}

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
    http.get('/api/v1/inventory/spools', () => HttpResponse.json(SPOOLS)),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/products', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/products/orders', () =>
      HttpResponse.json([orderLine(1, 'pending'), orderLine(2, 'received')]),
    ),
    http.get('/api/v1/inventory/products/variants/12/reorder-line', () => HttpResponse.json(REORDER_LINE)),
    http.get('/api/v1/printers/', () => HttpResponse.json([])),
  );
}

function openAt(url: string) {
  window.history.replaceState({}, '', url);
}

describe('InventoryPage — reorder list', () => {
  beforeEach(() => {
    localStorage.clear();
    setupHandlers();
  });

  it('shows the reorder list as its own section, with its lines counted on the tab', async () => {
    openAt('/inventory?section=orders');
    render(<InventoryPageRouter />);

    expect(await screen.findByRole('region', { name: 'To order' })).toBeInTheDocument();
    const tab = screen.getByRole('button', { name: /^Reorder list/ });
    expect(tab).toHaveAttribute('aria-current', 'page');
    await waitFor(() => expect(within(tab).getByText('2')).toBeInTheDocument());
    expect(screen.queryByText('Total Inventory')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Add Spool/ })).not.toBeInTheDocument();
  });

  it('keeps the reorder list in the URL', async () => {
    const user = userEvent.setup();
    openAt('/inventory');
    render(<InventoryPageRouter />);
    await screen.findByText('Total Inventory');

    await user.click(screen.getByRole('button', { name: /^Reorder list/ }));

    await waitFor(() => expect(window.location.search).toContain('section=orders'));
    expect(await screen.findByRole('region', { name: 'To book in' })).toBeInTheDocument();
  });

  it('reorders a spool of a product from its table row', async () => {
    const user = userEvent.setup();
    openAt('/inventory');
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByText('PETG').length).toBeGreaterThan(0));

    // Only the spool that belongs to a product can be reordered.
    const buttons = screen.getAllByRole('button', { name: 'Reorder' });
    expect(buttons).toHaveLength(1);
    await user.click(buttons[0]);

    const dialog = await screen.findByRole('dialog', { name: 'Reorder' });
    expect(await within(dialog).findByText('eSun PETG')).toBeInTheDocument();
    expect(within(dialog).getByText('Blue · 1 kg')).toBeInTheDocument();
  });

  it('reorders a spool of a product from its card', async () => {
    const user = userEvent.setup();
    openAt('/inventory');
    render(<InventoryPageRouter />);
    await waitFor(() => expect(screen.getAllByText('PETG').length).toBeGreaterThan(0));

    await user.click(screen.getByRole('button', { name: /^Cards$/ }));
    const buttons = await screen.findAllByRole('button', { name: 'Reorder' });
    expect(buttons).toHaveLength(1);
    await user.click(buttons[0]);

    expect(await screen.findByRole('dialog', { name: 'Reorder' })).toBeInTheDocument();
  });
});
