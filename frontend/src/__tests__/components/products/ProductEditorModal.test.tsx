/**
 * Product editor (#3165): the target stock per combination, the manufacturer's
 * price at the size and the suppliers — only where the product was bought —
 * travel in the save document.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
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
  presets: [],
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  price_date: null,
  sizes: [{ id: 10, label_weight: 1000, core_weight: 250, core_weight_catalog_id: null, price: 20, price_vat_included: true }],
  colors: [
    { id: 1, color_name: 'Black', rgba: '000000FF', extra_colors: null, effect_type: null },
    { id: 2, color_name: 'White', rgba: 'FFFFFFFF', extra_colors: null, effect_type: null },
  ],
  variants: [variant(11, 1, 2), variant(12, 2, null)],
  suppliers: [{ supplier_id: 7, supplier_name: 'Filament Shop', preferred: true }],
  spool_count: 0,
  remaining_g: 0,
};

describe('ProductEditorModal — targets', () => {
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

describe("ProductEditorModal — the manufacturer's price, the suppliers and the preset", () => {
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

  it('takes the price at the size, whatever the suppliers', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(
      <ProductEditorModal
        product={withSuppliers([{ supplier_id: 7, supplier_name: 'Filament Shop', preferred: true }])}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );

    const price = screen.getByRole('textbox', { name: 'List price' });
    expect(price).toHaveValue('20');
    // A supplier is only where the product was bought: no price, no number.
    expect(screen.queryByRole('textbox', { name: /1000 g/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: /Article/ })).not.toBeInTheDocument();

    await user.clear(price);
    await user.type(price, '22,5');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].sizes[0].price).toBe(22.5);
    expect(saved[0].suppliers).toEqual([{ supplier_id: 7, preferred: true }]);
  });

  it('adds a supplier, moves the star and hands it on when the usual one goes', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(
      <ProductEditorModal
        product={withSuppliers([{ supplier_id: 7, supplier_name: 'Filament Shop', preferred: true }])}
        onClose={vi.fn()}
        onSaved={onSaved}
      />,
    );

    await user.selectOptions(await screen.findByRole('combobox', { name: 'Add supplier' }), '8');
    expect(screen.getByRole('button', { name: 'Make Other Shop the usual supplier' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
    // Every supplier is on the product now, so there is nothing left to add.
    expect(screen.queryByRole('combobox', { name: 'Add supplier' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Make Other Shop the usual supplier' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(saved[0].suppliers).toEqual([
      { supplier_id: 7, preferred: false },
      { supplier_id: 8, preferred: true },
    ]);

    await user.click(screen.getByRole('button', { name: 'Remove Other Shop' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
    expect(saved[1].suppliers).toEqual([{ supplier_id: 7, preferred: true }]);
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

describe('ProductEditorModal — a preset per printer model', () => {
  let saved: FilamentProductInput[];
  const cloudPreset = (setting_id: string, name: string) => ({
    setting_id,
    name,
    type: 'filament',
    version: null,
    user_id: null,
    updated_time: null,
    is_custom: false,
  });
  const onTheH2S: FilamentProduct = {
    ...PRODUCT,
    slicer_filament: 'GFSA01_H2S',
    slicer_filament_name: 'Bambu PLA Matte @BBL H2S',
  };

  beforeEach(() => {
    saved = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/suppliers', () => HttpResponse.json([])),
      http.get('/api/v1/printers/', () =>
        HttpResponse.json([
          { id: 1, name: 'Links', model: 'H2S' },
          { id: 2, name: 'Rechts', model: 'H2D' },
        ]),
      ),
      http.get('/api/v1/slicer/printer-models', () => HttpResponse.json({})),
      http.get('/api/v1/cloud/status', () => HttpResponse.json({ is_authenticated: true })),
      http.get('/api/v1/cloud/filaments', () =>
        HttpResponse.json([
          cloudPreset('GFSA01_H2S', 'Bambu PLA Matte @BBL H2S'),
          cloudPreset('GFSA01_H2D_02', 'Bambu PLA Matte @BBL H2D 0.2 nozzle'),
          cloudPreset('GFSA01_H2D', 'Bambu PLA Matte @BBL H2D'),
        ]),
      ),
      http.put('/api/v1/inventory/products/1', async ({ request }) => {
        saved.push((await request.json()) as FilamentProductInput);
        return HttpResponse.json({ product: PRODUCT, spools_updated: 0 });
      }),
    );
  });

  it("adds one for another printer, starting on the own preset's variant for it", async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ProductEditorModal product={onTheH2S} onClose={vi.fn()} onSaved={onSaved} />);
    // Wait for the preset lists: the own preset is shown by its option then.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Slicer preset' })).not.toHaveTextContent('(GFSA01_H2S)'),
    );

    // The own preset is the H2S's, so only the H2D is offered.
    const add = await screen.findByRole('combobox', { name: 'Preset for a printer' });
    expect(within(add).queryByRole('option', { name: 'H2S' })).not.toBeInTheDocument();
    expect(screen.getByText('H2S')).toBeInTheDocument();
    await user.selectOptions(add, 'H2D');

    expect(screen.getByRole('button', { name: 'Preset for H2D' })).toBeInTheDocument();
    expect(screen.getByText('H2D')).toBeInTheDocument();
    // Every printer has a preset now.
    expect(screen.queryByRole('combobox', { name: 'Preset for a printer' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    // The plain one, not the one for the 0.2 nozzle.
    expect(saved[0].presets).toEqual([
      { printer_model: 'H2D', slicer_filament: 'GFSA01_H2D', slicer_filament_name: 'Bambu PLA Matte @BBL H2D' },
    ]);
  });

  it('keeps the ones it has, and a removed one goes', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    const product = {
      ...onTheH2S,
      presets: [{ printer_model: 'H2D', slicer_filament: 'GFSA01_H2D', slicer_filament_name: 'Bambu PLA Matte @BBL H2D' }],
    };
    render(<ProductEditorModal product={product} onClose={vi.fn()} onSaved={onSaved} />);

    await user.click(screen.getByRole('button', { name: 'Remove the preset for H2D' }));
    expect(await screen.findByRole('combobox', { name: 'Preset for a printer' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].presets).toEqual([]);
  });

  it('does not save a printer left without a preset', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    // No own preset: it stands for every printer, and nothing is picked ahead.
    render(<ProductEditorModal product={PRODUCT} onClose={vi.fn()} onSaved={onSaved} />);

    expect(screen.getByText('All printers')).toBeInTheDocument();
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Preset for a printer' }), 'H2S');
    expect(screen.getByRole('button', { name: 'Preset for H2S' })).toHaveTextContent('Choose a preset…');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].presets).toEqual([]);
  });
});

describe('ProductEditorModal — prices as of', () => {
  let saved: FilamentProductInput[];

  beforeEach(() => {
    saved = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/suppliers', () => HttpResponse.json([])),
      http.put('/api/v1/inventory/products/1', async ({ request }) => {
        saved.push((await request.json()) as FilamentProductInput);
        return HttpResponse.json({ product: PRODUCT, spools_updated: 0 });
      }),
    );
  });

  it('keeps the date the product has', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ProductEditorModal product={{ ...PRODUCT, price_date: '2026-09-01' }} onClose={vi.fn()} onSaved={onSaved} />);

    expect(screen.getByLabelText('Prices as of')).toHaveValue('2026-09-01');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(saved[0].price_date).toBe('2026-09-01');
  });

  it('sets today with one click', async () => {
    const onSaved = vi.fn();
    const user = userEvent.setup();
    render(<ProductEditorModal product={PRODUCT} onClose={vi.fn()} onSaved={onSaved} />);

    expect(screen.getByLabelText('Prices as of')).toHaveValue('');
    await user.click(screen.getByRole('button', { name: 'Today' }));
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    const now = new Date();
    const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    expect(saved[0].price_date).toBe(today);
  });
});
