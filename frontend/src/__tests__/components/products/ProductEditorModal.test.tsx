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
