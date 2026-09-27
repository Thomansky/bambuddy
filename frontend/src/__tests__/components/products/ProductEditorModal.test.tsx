/**
 * Product editor (#3165): the target stock per combination, and a supplier's
 * article number per colour × size, travel in the save document.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { ProductEditorModal } from '../../../components/products/ProductEditorModal';
import type { FilamentProduct, FilamentProductInput, FilamentVariant } from '../../../api/client';

function variant(id: number, colorId: number, minStock: number | null): FilamentVariant {
  return {
    id,
    color_id: colorId,
    size_id: 10,
    price_override: null,
    effective_price: 20,
    cost_per_kg: 20,
    codes: [],
    spool_count: 0,
    remaining_g: 0,
    min_stock: minStock,
    in_stock: 0,
    on_order: 0,
    shortfall: minStock ?? 0,
  };
}

const PRODUCT: FilamentProduct = {
  id: 1,
  label: 'Bambu Lab PLA Matte',
  brand: 'Bambu Lab',
  material: 'PLA',
  subtype: 'Matte',
  material_number: '52',
  slicer_filament: null,
  slicer_filament_name: null,
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  sizes: [{ id: 10, label_weight: 1000, core_weight: 250, core_weight_catalog_id: null, price: 20, price_vat_included: true }],
  colors: [
    { id: 1, color_name: 'Black', rgba: '000000FF', extra_colors: null, effect_type: null },
    { id: 2, color_name: 'White', rgba: 'FFFFFFFF', extra_colors: null, effect_type: null },
  ],
  variants: [variant(11, 1, 2), variant(12, 2, null)],
  suppliers: [
    {
      supplier_id: 7,
      supplier_name: 'Filament Shop',
      article_number: 'FS-PLA-M',
      preferred: true,
      prices: [],
      articles: [{ variant_id: 11, article_number: 'FS-BLK-1' }],
    },
  ],
  spool_count: 0,
  remaining_g: 0,
};

describe('ProductEditorModal — targets and article numbers', () => {
  let saved: FilamentProductInput[];

  beforeEach(() => {
    saved = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/suppliers', () => HttpResponse.json([{ id: 7, name: 'Filament Shop' }])),
      http.put('/api/v1/inventory/products/1', async ({ request }) => {
        saved.push((await request.json()) as FilamentProductInput);
        return HttpResponse.json({ product: PRODUCT, spools_updated: 0 });
      }),
    );
  });

  it('shows the targets and sets one for every combination', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ProductEditorModal product={PRODUCT} onClose={vi.fn()} onSaved={onSaved} />);

    await user.click(screen.getByRole('button', { name: 'Target stock' }));
    expect(screen.getByRole('textbox', { name: /Target stock \(spools\) Black/ })).toHaveValue('2');
    expect(screen.getByRole('textbox', { name: /Target stock \(spools\) White/ })).toHaveValue('');

    await user.type(screen.getByRole('textbox', { name: 'Set for all' }), '3');
    await user.click(screen.getByRole('button', { name: 'Set for all' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].variants.map((v) => [v.color_key, v.min_stock])).toEqual([
      ['c1', 3],
      ['c2', 3],
    ]);
  });

  it("keeps the supplier's article number per colour", async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ProductEditorModal product={PRODUCT} onClose={vi.fn()} onSaved={onSaved} />);

    await user.click(await screen.findByRole('button', { name: 'Article numbers per colour' }));
    expect(screen.getByRole('textbox', { name: 'Article no. Black 1000 g' })).toHaveValue('FS-BLK-1');
    const white = screen.getByRole('textbox', { name: 'Article no. White 1000 g' });
    expect(white).toHaveAttribute('placeholder', 'FS-PLA-M');
    await user.type(white, 'FS-WHT-1');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].suppliers[0].article_numbers).toEqual({ 'c1|s10': 'FS-BLK-1', 'c2|s10': 'FS-WHT-1' });
  });
});

describe('ProductEditorModal — setting a product up', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/suppliers', () => HttpResponse.json([])),
    );
  });

  const weights = () => screen.getAllByRole('textbox', { name: 'Net weight' }).map((input) => (input as HTMLInputElement).value);

  it('keeps the sizes in weight order', async () => {
    const user = userEvent.setup();
    render(<ProductEditorModal product={PRODUCT} onClose={vi.fn()} onSaved={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: '+ 5 kg' }));
    await user.click(screen.getByRole('button', { name: '+ 250 g' }));
    expect(weights()).toEqual(['250', '1000', '5000']);

    // A size typed in by hand settles into its place once the field is left.
    await user.click(screen.getByRole('button', { name: 'Size' }));
    const typed = screen.getAllByRole('textbox', { name: 'Net weight' }).at(-1) as HTMLInputElement;
    await user.type(typed, '750');
    expect(weights()).toEqual(['250', '1000', '5000', '750']);
    await user.tab();
    expect(weights()).toEqual(['250', '750', '1000', '5000']);
  });

  it('opens the supplier list when there is no supplier yet', async () => {
    const user = userEvent.setup();
    render(<ProductEditorModal product={{ ...PRODUCT, suppliers: [] }} onClose={vi.fn()} onSaved={vi.fn()} />);

    await user.click(await screen.findByRole('button', { name: 'Create suppliers' }));

    expect(await screen.findByRole('dialog', { name: 'Suppliers' })).toBeInTheDocument();
  });
});

describe('ProductEditorModal — one place per price, and the preset list', () => {
  let saved: FilamentProductInput[];

  const withSuppliers = (suppliers: FilamentProduct['suppliers']): FilamentProduct => ({
    ...PRODUCT,
    sizes: [{ ...PRODUCT.sizes[0], price: 20 }],
    suppliers,
  });

  beforeEach(() => {
    saved = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/suppliers', () =>
        HttpResponse.json([
          { id: 7, name: 'Filament Shop' },
          { id: 8, name: 'Other Shop' },
        ]),
      ),
      http.get('/api/v1/cloud/builtin-filaments', () =>
        HttpResponse.json([{ filament_id: 'GFA00', name: 'Bambu PLA Basic' }]),
      ),
      http.put('/api/v1/inventory/products/1', async ({ request }) => {
        saved.push((await request.json()) as FilamentProductInput);
        return HttpResponse.json({ product: PRODUCT, spools_updated: 0 });
      }),
    );
  });

  it("shows the usual supplier's price at the size and takes it from the supplier table", async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(
      <ProductEditorModal
        product={withSuppliers([
          { supplier_id: 7, supplier_name: 'Filament Shop', article_number: null, preferred: true, prices: [{ size_id: 10, price: 18.5 }], articles: [] },
        ])}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );

    // No price field at the size: it is the supplier's.
    expect(screen.queryByRole('textbox', { name: 'Price per spool' })).not.toBeInTheDocument();
    expect((await screen.findAllByText('€18.50')).length).toBeGreaterThan(0);
    const atSupplier = screen.getByRole('textbox', { name: 'Price per spool 1000 g' });
    expect(atSupplier).toHaveValue('18.5');

    await user.clear(atSupplier);
    await user.type(atSupplier, '17');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].sizes[0].price).toBe(17);
    expect(saved[0].suppliers[0].prices).toEqual({ s10: 17 });
  });

  it("brings the new usual supplier's prices in when the star moves", async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(
      <ProductEditorModal
        product={withSuppliers([
          { supplier_id: 7, supplier_name: 'Filament Shop', article_number: null, preferred: true, prices: [{ size_id: 10, price: 18.5 }], articles: [] },
          { supplier_id: 8, supplier_name: 'Other Shop', article_number: null, preferred: false, prices: [{ size_id: 10, price: 16 }], articles: [] },
        ])}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );

    await user.click(screen.getAllByRole('radio', { name: 'Usual supplier' })[1]);
    expect((await screen.findAllByText('€16.00')).length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].sizes[0].price).toBe(16);
    const bySupplier = Object.fromEntries(saved[0].suppliers.map((row) => [row.supplier_id, row]));
    expect(bySupplier[7]).toMatchObject({ preferred: false, prices: { s10: 18.5 } });
    expect(bySupplier[8]).toMatchObject({ preferred: true, prices: { s10: 16 } });
  });

  it('asks for the price at the size while the product has no supplier', async () => {
    render(<ProductEditorModal product={withSuppliers([])} onClose={vi.fn()} onSaved={vi.fn()} />);

    expect(screen.getByRole('textbox', { name: 'Price per spool' })).toHaveValue('20');
  });

  it('picks the slicer preset from the list the spool dialog offers', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ProductEditorModal product={withSuppliers([])} onClose={vi.fn()} onSaved={onSaved} />);

    await user.click(screen.getByRole('button', { name: 'Slicer preset' }));
    await user.click(await screen.findByRole('option', { name: /Bambu PLA Basic/ }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].slicer_filament).toBe('GFA00');
    expect(saved[0].slicer_filament_name).toBe('Bambu PLA Basic');
  });
});
