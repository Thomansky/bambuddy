/**
 * Reorder one combination on purpose — from a spool or a stock-matrix cell —
 * whatever its target stock: how many, from whom and what for. The line goes
 * onto the reorder list's "to order" column.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { ReorderLineModal } from '../../../components/products/ReorderLineModal';
import type { ProductReorderLine } from '../../../api/client';

const LINE: ProductReorderLine = {
  variant_id: 12,
  product_id: 1,
  product_label: 'Bambu Lab PLA Matte',
  material_number: '52',
  color_name: 'White',
  rgba: 'FFFFFFFF',
  extra_colors: null,
  effect_type: null,
  label_weight: 1000,
  refill: false,
  min_stock: null,
  spools: 1,
  in_stock: 1,
  on_order: 2,
  shortfall: 0,
  list_price: 20,
  price_vat_included: true,
  suppliers: [
    { supplier_id: 7, supplier_name: 'Filament Shop', preferred: true },
    { supplier_id: 8, supplier_name: 'Other Shop', preferred: false },
  ],
};

describe('ReorderLineModal', () => {
  let posted: unknown[];

  beforeEach(() => {
    posted = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/products/variants/12/reorder-line', () => HttpResponse.json(LINE)),
      http.post('/api/v1/inventory/products/reorder', async ({ request }) => {
        posted.push(await request.json());
        return HttpResponse.json({ added: 1, merged: 0 });
      }),
    );
  });

  it('shows the combination with its stock and starts from the usual supplier', async () => {
    render(<ReorderLineModal variantId={12} onClose={vi.fn()} />);

    expect(await screen.findByText('Bambu Lab PLA Matte')).toBeInTheDocument();
    expect(screen.getByText('White · 1 kg')).toBeInTheDocument();
    expect(screen.getByText('In stock 1 · ordered 2')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: /^Supplier/ })).toHaveValue('7');
    expect(screen.getByText('Total: €20.00')).toBeInTheDocument();
  });

  it('puts the line on the reorder list with its quantity, supplier and purpose', async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<ReorderLineModal variantId={12} onClose={onClose} />);
    await screen.findByText('Bambu Lab PLA Matte');

    const quantity = screen.getByRole('textbox', { name: /^Spools/ });
    await user.clear(quantity);
    await user.type(quantity, '3');
    await user.selectOptions(screen.getByRole('combobox', { name: /^Supplier/ }), '8');
    await user.type(screen.getByRole('textbox', { name: /For/ }), '  Job 4019 ');
    expect(screen.getByText('Total: €60.00')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Put on the reorder list' }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(posted).toEqual([{ items: [{ variant_id: 12, quantity: 3, supplier_id: 8, reference: 'Job 4019' }] }]);
  });

  it('sends no supplier and no purpose when none is given', async () => {
    const user = userEvent.setup();
    render(<ReorderLineModal variantId={12} onClose={vi.fn()} />);
    await screen.findByText('Bambu Lab PLA Matte');

    await user.selectOptions(screen.getByRole('combobox', { name: /^Supplier/ }), '');
    await user.click(screen.getByRole('button', { name: 'Put on the reorder list' }));

    await waitFor(() =>
      expect(posted).toEqual([{ items: [{ variant_id: 12, quantity: 1, supplier_id: null, reference: null }] }]),
    );
  });

  it('shows the most it takes instead of a bigger number it would not send', async () => {
    const user = userEvent.setup();
    render(<ReorderLineModal variantId={12} onClose={vi.fn()} />);
    await screen.findByText('Bambu Lab PLA Matte');

    const quantity = screen.getByRole('textbox', { name: /^Spools/ });
    await user.clear(quantity);
    await user.type(quantity, '250');

    expect(quantity).toHaveValue('100');
  });

  it('will not add a line of zero spools', async () => {
    const user = userEvent.setup();
    render(<ReorderLineModal variantId={12} onClose={vi.fn()} />);
    await screen.findByText('Bambu Lab PLA Matte');

    await user.clear(screen.getByRole('textbox', { name: /^Spools/ }));

    expect(screen.getByRole('button', { name: 'Put on the reorder list' })).toBeDisabled();
  });

  it('says so when the combination cannot be loaded', async () => {
    server.use(
      http.get('/api/v1/inventory/products/variants/12/reorder-line', () =>
        HttpResponse.json({ detail: 'Variant not found' }, { status: 404 }),
      ),
    );
    render(<ReorderLineModal variantId={12} onClose={vi.fn()} />);

    expect(await screen.findByText('Could not load this combination.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Put on the reorder list' })).toBeDisabled();
  });
});
