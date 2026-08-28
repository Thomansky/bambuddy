/**
 * Tests for the InventoryPage stats bar — the Stock Value tile.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

let nextId = 1;

function makeSpool(overrides: Record<string, unknown> = {}) {
  const id = nextId++;
  return {
    id,
    material: 'PLA',
    subtype: 'Matte',
    color_name: 'Charcoal',
    rgba: '000000FF',
    extra_colors: null,
    effect_type: null,
    brand: 'Bambu',
    label_weight: 1000,
    core_weight: 250,
    core_weight_catalog_id: null,
    weight_used: 0,
    weight_used_baseline: 0,
    slicer_filament: null,
    slicer_filament_name: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    note: null,
    added_full: true,
    last_used: null,
    encode_time: null,
    tag_uid: null,
    tray_uuid: null,
    data_origin: null,
    tag_type: null,
    archived_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    cost_per_kg: null,
    last_scale_weight: null,
    last_weighed_at: null,
    category: null,
    low_stock_threshold_pct: null,
    ...overrides,
  };
}

function stubInventory(spools: unknown[]) {
  server.use(
    http.get('/api/v1/settings/spoolman', () =>
      HttpResponse.json({
        spoolman_enabled: 'false',
        spoolman_url: '',
        spoolman_sync_mode: 'off',
        spoolman_disable_weight_sync: 'false',
        spoolman_report_partial_usage: 'false',
        auto_add_unknown_rfid: 'false',
      })
    ),
    http.get('/api/v1/inventory/spools', () => HttpResponse.json(spools))
  );
}

describe('InventoryPage stock value tile', () => {
  beforeEach(() => {
    nextId = 1;
  });

  it('prices remaining filament, shows the purchase total and the unpriced count', async () => {
    stubInventory([
      // 500 g of 25/kg left -> 12.50 remaining, 25.00 purchase
      makeSpool({ cost_per_kg: 25, weight_used: 500 }),
      // untouched 20/kg spool -> 20.00 remaining and purchase
      makeSpool({ cost_per_kg: 20 }),
      // no price -> counted, not silently valued at zero
      makeSpool(),
      // archived -> excluded entirely
      makeSpool({ cost_per_kg: 99, archived_at: '2024-02-01T00:00:00Z' }),
    ]);

    render(<InventoryPageRouter />);

    expect(await screen.findByText('Stock Value')).toBeInTheDocument();
    expect(screen.getByText('$32.50')).toBeInTheDocument();
    expect(screen.getByText(/Purchase value: \$45\.00/)).toBeInTheDocument();
    expect(screen.getByText(/1 without price/)).toBeInTheDocument();
  });

  it('omits the unpriced note when every active spool has a price', async () => {
    stubInventory([
      makeSpool({ cost_per_kg: 30, weight_used: 250 }),
    ]);

    render(<InventoryPageRouter />);

    expect(await screen.findByText('Stock Value')).toBeInTheDocument();
    expect(screen.getByText('$22.50')).toBeInTheDocument();
    expect(screen.queryByText(/without price/)).not.toBeInTheDocument();
  });
});
