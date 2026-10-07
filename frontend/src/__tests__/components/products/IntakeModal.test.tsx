/**
 * Goods-in (#3165): once a colour is picked, the product's standard size, the
 * one usually ordered, is offered first; a refill is named as one.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { IntakeModal } from '../../../components/products/IntakeModal';
import type { FilamentProduct, FilamentProductSize, FilamentVariant } from '../../../api/client';

function size(id: number, patch: Partial<FilamentProductSize>): FilamentProductSize {
  return {
    id,
    label_weight: 1000,
    core_weight: 250,
    core_weight_catalog_id: null,
    price: 20,
    price_vat_included: true,
    standard: false,
    refill: false,
    ...patch,
  };
}

function variant(id: number, sizeId: number): FilamentVariant {
  return {
    id,
    color_id: 1,
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
  };
}

function product(sizes: FilamentProductSize[]): FilamentProduct {
  return {
    id: 1,
    label: 'Bambu Lab PLA Basic',
    brand: 'Bambu Lab',
    material: 'PLA',
    subtype: 'Basic',
    material_number: null,
    slicer_filament: null,
    slicer_filament_name: null,
    presets: [],
    supports: [],
    note: null,
    price_date: null,
    sizes,
    colors: [{ id: 1, color_name: 'Black', rgba: '000000FF', extra_colors: null, effect_type: null }],
    variants: sizes.map((s) => variant(20 + s.id, s.id)),
    suppliers: [],
    spool_count: 0,
    remaining_g: 0,
  };
}

function serve(products: FilamentProduct[]) {
  server.use(
    http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
    http.get('/api/v1/inventory/products', () => HttpResponse.json(products)),
    http.get('/api/v1/inventory/locations', () => HttpResponse.json([])),
  );
}

describe('IntakeModal — the standard size', () => {
  beforeEach(() => {
    serve([product([size(10, {}), size(11, { price: 17, refill: true, standard: true })])]);
  });

  it('offers the standard size first once a colour is picked', async () => {
    const user = userEvent.setup();
    render(<IntakeModal onClose={vi.fn()} initialProductId={1} />);

    await user.click(await screen.findByRole('button', { name: /Black/ }));

    expect(screen.getByRole('button', { name: /^1 kg Refill/ })).toHaveClass('border-bambu-green');
    expect(screen.getByRole('button', { name: /^1 kg$/ })).not.toHaveClass('border-bambu-green');
  });

  it('opens a delivery from the reorder list on its combination and quantity', async () => {
    render(<IntakeModal onClose={vi.fn()} initialOrder={{ productId: 1, variantId: 30, quantity: 3 }} />);

    expect(await screen.findByText('Black · 1 kg')).toBeInTheDocument();
    expect(screen.getByDisplayValue('3')).toBeInTheDocument();
  });

  it('leaves the choice open without a standard size', async () => {
    serve([product([size(10, {}), size(11, { price: 17, refill: true })])]);
    const user = userEvent.setup();
    render(<IntakeModal onClose={vi.fn()} initialProductId={1} />);

    await user.click(await screen.findByRole('button', { name: /Black/ }));

    expect(screen.getByRole('button', { name: /^1 kg Refill/ })).not.toHaveClass('border-bambu-green');
    expect(screen.getByRole('button', { name: /^1 kg$/ })).not.toHaveClass('border-bambu-green');
  });
});
