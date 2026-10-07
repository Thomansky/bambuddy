/**
 * Reorder (#3165): the combinations below their target stock, grouped by the
 * supplier they are bought from, go onto the reorder list as ticked.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { ReorderModal } from '../../../components/products/ReorderModal';
import type { ProductReorderLine } from '../../../api/client';

function reorderLine(overrides: Partial<ProductReorderLine>): ProductReorderLine {
  return {
    variant_id: 11,
    product_id: 1,
    product_label: 'Bambu Lab PLA Matte',
    material_number: '52',
    color_name: 'Black',
    rgba: '000000FF',
    extra_colors: null,
    effect_type: null,
    label_weight: 1000,
    min_stock: 2,
    spools: 0,
    in_stock: 0,
    on_order: 0,
    shortfall: 2,
    list_price: 20,
    price_vat_included: true,
    suppliers: [],
    ...overrides,
  };
}

const LINES: ProductReorderLine[] = [
  reorderLine({
    suppliers: [
      { supplier_id: 7, supplier_name: 'Filament Shop', preferred: true },
      { supplier_id: 8, supplier_name: 'Other Shop', preferred: false },
    ],
  }),
  reorderLine({ variant_id: 12, color_name: 'White', rgba: 'FFFFFFFF', min_stock: 1, shortfall: 1 }),
];

describe('ReorderModal', () => {
  let posted: unknown[];

  beforeEach(() => {
    posted = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/products/reorder', () => HttpResponse.json(LINES)),
      http.post('/api/v1/inventory/products/reorder', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({ added: 2, merged: 0 });
      }),
    );
  });

  it('proposes the shortfall at the usual supplier, grouped by supplier', async () => {
    render(<ReorderModal onClose={vi.fn()} onDone={vi.fn()} />);

    const shop = (await screen.findByRole('heading', { name: 'Filament Shop' })).closest('section') as HTMLElement;
    expect(within(shop).getByRole('textbox', { name: /Spools .*Black/ })).toHaveValue('2');
    // Two spools at the manufacturer's price, whichever shop they come from.
    expect(within(shop).getByText('Total: €40.00')).toBeInTheDocument();

    const none = screen.getByRole('heading', { name: 'No supplier' }).closest('section') as HTMLElement;
    expect(within(none).getByRole('textbox', { name: /Spools .*White/ })).toHaveValue('1');
  });

  it('puts the ticked lines on the reorder list', async () => {
    const onDone = vi.fn();
    const user = userEvent.setup();
    render(<ReorderModal onClose={vi.fn()} onDone={onDone} />);
    await screen.findByRole('heading', { name: 'Filament Shop' });

    await user.click(screen.getByRole('button', { name: 'Put 2 lines on the reorder list' }));

    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(posted).toEqual([
      {
        items: [
          { variant_id: 11, quantity: 2, supplier_id: 7 },
          { variant_id: 12, quantity: 1, supplier_id: null },
        ],
      },
    ]);
  });

  it('leaves an unticked line off and moves a line to another supplier', async () => {
    const user = userEvent.setup();
    render(<ReorderModal onClose={vi.fn()} onDone={vi.fn()} />);
    await screen.findByRole('heading', { name: 'Filament Shop' });

    await user.click(screen.getByRole('checkbox', { name: /White/ }));
    await user.selectOptions(screen.getByRole('combobox', { name: /Supplier .*Black/ }), '8');
    const quantity = screen.getByRole('textbox', { name: /Spools .*Black/ });
    await user.clear(quantity);
    await user.type(quantity, '3');

    expect(screen.getByRole('heading', { name: 'Other Shop' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Filament Shop' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Put 1 line on the reorder list' }));

    await waitFor(() => expect(posted).toEqual([{ items: [{ variant_id: 11, quantity: 3, supplier_id: 8 }] }]));
  });

  it('says so when everything is at its target stock', async () => {
    server.use(http.get('/api/v1/inventory/products/reorder', () => HttpResponse.json([])));
    render(<ReorderModal onClose={vi.fn()} onDone={vi.fn()} />);

    expect(await screen.findByText('Everything is at its target stock.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reorder list/ })).toBeDisabled();
  });
});
