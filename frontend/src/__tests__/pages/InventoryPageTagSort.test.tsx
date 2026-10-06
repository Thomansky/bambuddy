/**
 * Sorting the Inventory by its Tag ID column.
 *
 * Asked for to see which spools still have to be scanned: the column showed
 * each spool's tag but had no sort key, so its header did nothing. Ascending
 * puts the spools without a tag first, descending puts the tagged ones first.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { render } from '../utils';
import InventoryPageRouter from '../../pages/InventoryPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const baseSpool = {
  subtype: null,
  color_name: 'Black',
  rgba: '000000FF',
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
  tag_uid: null as string | null,
  tray_uuid: null as string | null,
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
  material: 'PLA',
  weight_used: 0,
};

// The brand names the row, so the rendered order reads in an assertion.
const spool = (id: number, brand: string, tags: { tag_uid?: string; tray_uuid?: string } = {}) => ({
  ...baseSpool,
  id,
  brand,
  ...tags,
});

// API order deliberately mixes tagged and untagged spools.
const SPOOLS = [
  spool(1, 'Tagged Two', { tag_uid: '04D3AE73D32A81' }),
  spool(2, 'Untagged First', {}),
  spool(3, 'Bambu Tray', { tray_uuid: '7E82F1090910430C925EAD2BE4AB40F8' }),
  spool(4, 'Tagged One', { tag_uid: '0447BD73D32A81' }),
  spool(5, 'Untagged Second', {}),
];

const MOCK_SETTINGS = {
  currency: 'USD',
  language: 'en',
  date_format: 'system',
  time_format: 'system',
  low_stock_threshold: 20.0,
  spoolman_enabled: false,
  spoolman_url: '',
};

function setupHandlers() {
  server.use(
    http.get('/api/v1/settings/', () => HttpResponse.json(MOCK_SETTINGS)),
    http.get('/api/v1/settings/spoolman', () => HttpResponse.json({ spoolman_enabled: 'false', spoolman_url: '' })),
    http.get('/api/v1/inventory/spools', () => HttpResponse.json(SPOOLS)),
    http.get('/api/v1/inventory/assignments', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/catalog', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/color-catalog', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/colors', () => HttpResponse.json([])),
    http.get('/api/v1/inventory/spool-catalog', () => HttpResponse.json([])),
    http.get('/api/v1/printers/', () => HttpResponse.json([])),
  );
}

/** Table body rows, header excluded. */
const dataRows = () => screen.getAllByRole('row').slice(1);

/** Position of the row whose text contains ``name``, or -1. */
const rowIndexOf = (name: string) => dataRows().findIndex((row) => (row.textContent ?? '').includes(name));

const order = (names: string[]) => names.map(rowIndexOf);
const isAscending = (positions: number[]) =>
  positions.every((p) => p >= 0) && positions.every((p, i) => i === 0 || positions[i - 1] < p);

describe('InventoryPage — sorting by Tag ID', () => {
  beforeEach(() => {
    setupHandlers();
    // Tag ID is hidden by default; the stored column config turns it on. No
    // stored sort, so every test starts in the order the API returned.
    vi.mocked(localStorage.getItem).mockImplementation((key: string) =>
      key === 'bambuddy-inventory-columns' ? JSON.stringify([{ id: 'tag_id', label: 'Tag ID', visible: true }]) : null,
    );
  });

  it('puts the spools still to be scanned first', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(dataRows().length).toBe(SPOOLS.length));

    fireEvent.click(screen.getByRole('columnheader', { name: /^tag id$/i }));

    await waitFor(() =>
      expect(
        isAscending(order(['Untagged First', 'Untagged Second', 'Tagged One', 'Tagged Two', 'Bambu Tray'])),
      ).toBe(true),
    );
  });

  it('puts the tagged spools first on a second click', async () => {
    render(<InventoryPageRouter />);
    await waitFor(() => expect(dataRows().length).toBe(SPOOLS.length));
    const header = screen.getByRole('columnheader', { name: /^tag id$/i });

    fireEvent.click(header);
    fireEvent.click(header);

    await waitFor(() =>
      expect(
        isAscending(order(['Bambu Tray', 'Tagged Two', 'Tagged One', 'Untagged First', 'Untagged Second'])),
      ).toBe(true),
    );
  });
});
