/**
 * The Products section (#3165) is built like the spool list: search, filter
 * chips, configurable and sortable columns, and a row unfolds into the stock
 * of each colour × size.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { ProductsPanel } from '../../../components/products/ProductsPanel';
import type { FilamentProduct, FilamentVariant } from '../../../api/client';

function variant(id: number, colorId: number, sizeId: number, patch: Partial<FilamentVariant> = {}): FilamentVariant {
  return {
    id,
    color_id: colorId,
    size_id: sizeId,
    price_override: null,
    effective_price: 20,
    cost_per_kg: 20,
    codes: [],
    spool_count: 0,
    remaining_g: 0,
    min_stock: null,
    in_stock: 0,
    on_order: 0,
    shortfall: 0,
    ...patch,
  };
}

function product(patch: Partial<FilamentProduct>): FilamentProduct {
  return {
    id: 1,
    label: '',
    brand: null,
    material: 'PLA',
    subtype: null,
    material_number: null,
    slicer_filament: null,
    slicer_filament_name: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    note: null,
    sizes: [],
    colors: [],
    variants: [],
    suppliers: [],
    spool_count: 0,
    remaining_g: 0,
    ...patch,
  };
}

const PRODUCTS: FilamentProduct[] = [
  product({
    id: 1,
    label: 'Bambu Lab PLA Matte',
    brand: 'Bambu Lab',
    subtype: 'Matte',
    material_number: '52',
    sizes: [{ id: 10, label_weight: 1000, core_weight: 250, core_weight_catalog_id: 1, price: 20, price_vat_included: true }],
    colors: [
      { id: 1, color_name: 'Black', rgba: '000000FF', extra_colors: null, effect_type: null },
      { id: 2, color_name: 'White', rgba: 'FFFFFFFF', extra_colors: null, effect_type: null },
    ],
    variants: [
      variant(11, 1, 10, { spool_count: 2, remaining_g: 1800, in_stock: 2 }),
      variant(12, 2, 10, { min_stock: 2, shortfall: 2 }),
    ],
    suppliers: [
      { supplier_id: 7, supplier_name: 'Shop A', preferred: true },
    ],
    spool_count: 2,
    remaining_g: 1800,
  }),
  product({
    id: 2,
    label: 'Sunlu PETG',
    brand: 'Sunlu',
    material: 'PETG',
    material_number: '7',
    sizes: [
      { id: 20, label_weight: 1000, core_weight: 250, core_weight_catalog_id: 2, price: 15, price_vat_included: true },
      { id: 21, label_weight: 5000, core_weight: 900, core_weight_catalog_id: 2, price: 70, price_vat_included: true },
    ],
    colors: [{ id: 3, color_name: 'Grey', rgba: '808080FF', extra_colors: null, effect_type: null }],
    variants: [variant(21, 3, 20), variant(22, 3, 21)],
    suppliers: [{ supplier_id: 8, supplier_name: 'Shop B', preferred: true }],
  }),
  product({
    id: 3,
    label: 'eSun ABS',
    brand: 'eSun',
    material: 'ABS',
    sizes: [{ id: 30, label_weight: 1000, core_weight: 250, core_weight_catalog_id: null, price: null, price_vat_included: true }],
    colors: [{ id: 4, color_name: 'Red', rgba: 'FF0000FF', extra_colors: null, effect_type: null }],
    variants: [variant(31, 4, 30, { spool_count: 1, remaining_g: 400, in_stock: 1 })],
    spool_count: 1,
    remaining_g: 400,
  }),
];

/** The brands of the listed rows, top to bottom. */
function listedBrands(): string[] {
  const table = screen.getByRole('table');
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => ['Bambu Lab', 'Sunlu', 'eSun'].find((brand) => row.textContent?.includes(brand)) ?? '?');
}

function renderPanel() {
  return render(<ProductsPanel onIntake={vi.fn()} onEdit={vi.fn()} onConvert={vi.fn()} />);
}

describe('ProductsPanel', () => {
  beforeEach(() => {
    localStorage.clear();
    server.use(
      http.get('/api/v1/inventory/products', () => HttpResponse.json(PRODUCTS)),
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/catalog', () =>
        HttpResponse.json([
          { id: 1, name: 'Bambu reusable spool', weight: 250, is_default: false },
          { id: 2, name: 'Cardboard spool', weight: 200, is_default: false },
        ]),
      ),
    );
  });

  it('lists the products in material-number order with the default columns', async () => {
    renderPanel();

    await screen.findByRole('table');
    expect(listedBrands()).toEqual(['Sunlu', 'Bambu Lab', 'eSun']);
    const header = within(screen.getByRole('table')).getAllByRole('columnheader').map((th) => th.textContent);
    expect(header).toEqual(
      expect.arrayContaining(['No.', 'Manufacturer', 'Material', 'Type', 'Supplier', 'Spools', 'Stock', 'Missing']),
    );
    expect(screen.getByText('3 products')).toBeInTheDocument();
  });

  it('filters by supplier, spool size and spool type', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole('table');

    await user.selectOptions(screen.getByRole('combobox', { name: 'Supplier' }), '8');
    expect(listedBrands()).toEqual(['Sunlu']);

    await user.click(screen.getByRole('button', { name: /Clear filters/ }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Spool size' }), '5000');
    expect(listedBrands()).toEqual(['Sunlu']);

    await user.click(screen.getByRole('button', { name: /Clear filters/ }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Spool type' }), '1');
    expect(listedBrands()).toEqual(['Bambu Lab']);
  });

  it('filters products without a supplier and those below target', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole('table');

    await user.selectOptions(screen.getByRole('combobox', { name: 'Supplier' }), '__none__');
    expect(listedBrands()).toEqual(['eSun']);

    await user.click(screen.getByRole('button', { name: /Clear filters/ }));
    await user.click(screen.getByRole('button', { name: 'Below target' }));
    expect(listedBrands()).toEqual(['Bambu Lab']);
  });

  it('finds a product by where it was bought', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole('table');

    await user.type(screen.getByPlaceholderText(/Search products/), 'Shop A');

    expect(listedBrands()).toEqual(['Bambu Lab']);
  });

  it('sorts by a column, then back to the number order', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole('table');
    const spoolsHeader = () => within(screen.getByRole('table')).getByRole('columnheader', { name: /Spools/ });

    await user.click(spoolsHeader());
    expect(listedBrands()).toEqual(['Sunlu', 'eSun', 'Bambu Lab']);
    await user.click(spoolsHeader());
    expect(listedBrands()).toEqual(['Bambu Lab', 'eSun', 'Sunlu']);
    await user.click(spoolsHeader());
    expect(listedBrands()).toEqual(['Sunlu', 'Bambu Lab', 'eSun']);
  });

  it('marks what is missing and unfolds a row into its colours', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole('table');

    const table = screen.getByRole('table');
    expect(within(table).getByText('−2')).toBeInTheDocument();
    await user.click(within(table).getByText('Bambu Lab'));

    await waitFor(() => expect(screen.getByText('White')).toBeInTheDocument());
    expect(screen.getByText('Black')).toBeInTheDocument();
  });

  it('offers the columns in the same dialog as the spool list', async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByRole('table');

    await user.click(screen.getByRole('button', { name: /Columns/ }));

    const dialog = (await screen.findByText('Nozzle temp.')).closest('div.fixed') as HTMLElement;
    expect(within(dialog).getByText('Spool type')).toBeInTheDocument();
    expect(within(dialog).getByText('Manufacturer')).toBeInTheDocument();
  });
});
