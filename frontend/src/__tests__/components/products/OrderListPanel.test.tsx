/**
 * The reorder list: what should be ordered, what has been ordered, and what
 * has arrived and still has to be booked in — three columns, a line moves on
 * with one click and goods-in books it in.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../../utils';
import { server } from '../../mocks/server';
import { OrderListPanel } from '../../../components/products/OrderListPanel';
import type { ProductOrderLine } from '../../../api/client';

function orderLine(overrides: Partial<ProductOrderLine>): ProductOrderLine {
  return {
    id: 1,
    status: 'pending',
    quantity: 1,
    reference: null,
    priority: 'normal',
    note: '1 kg',
    added_at: '2026-10-01T08:00:00',
    purchased_at: null,
    received_at: null,
    supplier_id: null,
    supplier_name: null,
    material: 'PLA',
    subtype: 'Matte',
    brand: 'Bambu Lab',
    color_name: 'Black',
    variant_id: 11,
    product_id: 1,
    product_label: 'Bambu Lab PLA Matte',
    material_number: '52',
    rgba: '000000FF',
    extra_colors: null,
    effect_type: null,
    label_weight: 1000,
    refill: false,
    list_price: 20,
    price_vat_included: true,
    suppliers: [
      { supplier_id: 7, supplier_name: 'Filament Shop', preferred: true },
      { supplier_id: 8, supplier_name: 'Other Shop', preferred: false },
    ],
    ...overrides,
  };
}

const LINES: ProductOrderLine[] = [
  orderLine({ id: 1, quantity: 2, supplier_id: 7, supplier_name: 'Filament Shop', reference: 'Job 4019' }),
  orderLine({ id: 2, variant_id: 12, color_name: 'White', rgba: 'FFFFFFFF', list_price: null }),
  orderLine({
    id: 3,
    status: 'purchased',
    variant_id: 13,
    color_name: 'Red',
    supplier_id: 7,
    supplier_name: 'Filament Shop',
    purchased_at: '2026-10-03T09:00:00',
  }),
  orderLine({
    id: 4,
    status: 'received',
    quantity: 3,
    variant_id: 14,
    color_name: 'Blue',
    purchased_at: '2026-10-03T09:00:00',
    received_at: '2026-10-06T10:00:00',
  }),
  // A line typed into the shopping list by hand: no product behind it.
  orderLine({
    id: 5,
    status: 'received',
    material: 'PETG',
    subtype: null,
    brand: 'Sunlu',
    color_name: 'Grey',
    variant_id: null,
    product_id: null,
    product_label: null,
    material_number: null,
    rgba: null,
    label_weight: null,
    list_price: null,
    suppliers: [],
  }),
];

function column(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

describe('OrderListPanel', () => {
  let patched: { id: string; body: unknown }[];
  let removed: string[];

  beforeEach(() => {
    patched = [];
    removed = [];
    server.use(
      http.get('/api/v1/settings/', () => HttpResponse.json({ currency: 'EUR' })),
      http.get('/api/v1/inventory/products/orders', () => HttpResponse.json(LINES)),
      http.patch('/api/v1/inventory/products/orders/:id', async ({ params, request }) => {
        patched.push({ id: String(params.id), body: await request.json() });
        return HttpResponse.json(LINES.find((line) => line.id === Number(params.id)));
      }),
      http.delete('/api/v1/inventory/shopping-list/:id', ({ params }) => {
        removed.push(String(params.id));
        return HttpResponse.json({ status: 'deleted' });
      }),
    );
  });

  it('sorts the lines into the three columns, to order grouped by supplier', async () => {
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });

    const toOrder = column('To order');
    expect(within(toOrder).getByText('2 lines · 3 spools')).toBeInTheDocument();
    // The shop's lines with what they cost, those without a supplier last.
    expect(within(toOrder).getByRole('heading', { name: 'Filament Shop' })).toBeInTheDocument();
    expect(within(toOrder).getByText('Total: €40.00')).toBeInTheDocument();
    expect(within(toOrder).getByRole('heading', { name: 'No supplier' })).toBeInTheDocument();
    expect(within(toOrder).getByDisplayValue('Job 4019')).toBeInTheDocument();

    const ordered = column('Ordered');
    expect(within(ordered).getByText('Red · 1 kg')).toBeInTheDocument();
    expect(within(ordered).getByText(/^Ordered /)).toBeInTheDocument();

    const toBookIn = column('To book in');
    expect(within(toBookIn).getByText('Blue · 1 kg')).toBeInTheDocument();
    expect(within(toBookIn).getByText('Sunlu PETG')).toBeInTheDocument();
    expect(within(toBookIn).getByText(/^Arrived /)).toBeInTheDocument();
  });

  it('moves a line on: ordered, then arrived, and back', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });

    const first = screen.getByTestId('order-line-1');
    await user.click(within(first).getByRole('button', { name: 'Ordered' }));
    await user.click(within(screen.getByTestId('order-line-3')).getByRole('button', { name: 'Arrived' }));
    await user.click(within(screen.getByTestId('order-line-4')).getByRole('button', { name: 'Back' }));

    await waitFor(() =>
      expect(patched).toEqual([
        { id: '1', body: { status: 'purchased' } },
        { id: '3', body: { status: 'received' } },
        { id: '4', body: { status: 'purchased' } },
      ]),
    );
  });

  it('books a delivered line in through goods-in, and ticks off a line without a product', async () => {
    const onBookIn = vi.fn();
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={onBookIn} />);
    await screen.findByRole('region', { name: 'To order' });

    await user.click(within(screen.getByTestId('order-line-4')).getByRole('button', { name: 'Book in' }));
    expect(onBookIn).toHaveBeenCalledWith(expect.objectContaining({ id: 4, variant_id: 14, quantity: 3 }));

    const plain = screen.getByTestId('order-line-5');
    expect(within(plain).queryByRole('button', { name: 'Book in' })).not.toBeInTheDocument();
    await user.click(within(plain).getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(removed).toEqual(['5']));
  });

  it('changes quantity, purpose and supplier in place', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });
    const line = screen.getByTestId('order-line-1');

    const quantity = within(line).getByRole('textbox', { name: 'Number of spools' });
    await user.clear(quantity);
    await user.type(quantity, '5{Enter}');
    const reference = within(line).getByDisplayValue('Job 4019');
    await user.clear(reference);
    await user.type(reference, 'Customer X{Enter}');
    await user.selectOptions(within(line).getByRole('combobox', { name: 'Supplier' }), '8');

    await waitFor(() =>
      expect(patched).toEqual([
        { id: '1', body: { quantity: 5 } },
        { id: '1', body: { reference: 'Customer X' } },
        { id: '1', body: { supplier_id: 8 } },
      ]),
    );
  });

  it('leaves a quantity it cannot take as it was', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });
    const quantity = within(screen.getByTestId('order-line-1')).getByRole('textbox', { name: 'Number of spools' });

    await user.clear(quantity);
    await user.type(quantity, '0{Enter}');

    expect(quantity).toHaveValue('2');
    expect(patched).toEqual([]);
  });

  it('goes back to the saved quantity when the change fails', async () => {
    server.use(
      http.patch('/api/v1/inventory/products/orders/:id', () =>
        HttpResponse.json({ detail: 'Server error' }, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });
    const line = screen.getByTestId('order-line-1');

    const quantity = within(line).getByRole('textbox', { name: 'Number of spools' });
    await user.clear(quantity);
    await user.type(quantity, '5{Enter}');

    await waitFor(() =>
      expect(within(line).getByRole('textbox', { name: 'Number of spools' })).toHaveValue('2'),
    );
  });

  it('saves an edited quantity and still moves the line on the click that ended the edit', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });
    const line = screen.getByTestId('order-line-1');

    const quantity = within(line).getByRole('textbox', { name: 'Number of spools' });
    await user.clear(quantity);
    await user.type(quantity, '5');
    await user.click(within(line).getByRole('button', { name: 'Ordered' }));

    await waitFor(() =>
      expect(patched).toEqual([
        { id: '1', body: { quantity: 5 } },
        { id: '1', body: { status: 'purchased' } },
      ]),
    );
  });

  it('sends a line without a product to the forecast to be received', async () => {
    server.use(
      http.get('/api/v1/inventory/products/orders', () =>
        HttpResponse.json([{ ...LINES[4], status: 'purchased', received_at: null }]),
      ),
    );
    render(<OrderListPanel onBookIn={vi.fn()} />);

    const line = await screen.findByTestId('order-line-5');
    expect(within(line).queryByRole('button', { name: 'Arrived' })).not.toBeInTheDocument();
    expect(within(line).getByText('Receive in the forecast')).toBeInTheDocument();
  });

  it('puts urgent lines first, with their supplier group, and marks them', async () => {
    server.use(
      http.get('/api/v1/inventory/products/orders', () =>
        HttpResponse.json([
          orderLine({ id: 1, supplier_id: 7, supplier_name: 'Alpha Shop', priority: 'low' }),
          orderLine({ id: 2, supplier_id: 7, supplier_name: 'Alpha Shop' }),
          orderLine({ id: 3, supplier_id: 8, supplier_name: 'Zeta Shop', color_name: 'Red', priority: 'high' }),
          orderLine({ id: 4, status: 'purchased', color_name: 'Blue' }),
          orderLine({ id: 5, status: 'purchased', color_name: 'Green', priority: 'high' }),
        ]),
      ),
    );
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });

    const ids = (scope: HTMLElement) =>
      within(scope)
        .getAllByTestId(/^order-line-/)
        .map((el) => el.getAttribute('data-testid'));
    // The shop with the urgent line comes first, though Alpha sorts before Zeta.
    const groups = within(column('To order')).getAllByRole('heading', { level: 4 }).map((h) => h.textContent);
    expect(groups).toEqual(['Zeta Shop', 'Alpha Shop']);
    expect(ids(column('To order'))).toEqual(['order-line-3', 'order-line-2', 'order-line-1']);
    expect(ids(column('Ordered'))).toEqual(['order-line-5', 'order-line-4']);

    expect(within(screen.getByTestId('order-line-3')).getByTestId('priority-badge')).toHaveTextContent('High');
    expect(within(screen.getByTestId('order-line-1')).getByTestId('priority-badge')).toHaveTextContent('Low');
    expect(within(screen.getByTestId('order-line-2')).queryByTestId('priority-badge')).not.toBeInTheDocument();
  });

  it('changes a line\'s priority', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });

    await user.selectOptions(
      within(screen.getByTestId('order-line-2')).getByRole('combobox', { name: 'Priority' }),
      'high',
    );

    await waitFor(() => expect(patched).toEqual([{ id: '2', body: { priority: 'high' } }]));
  });

  it('shows the chosen priority at once and steps on from it', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });
    const select = within(screen.getByTestId('order-line-2')).getByRole('combobox', { name: 'Priority' });

    await user.selectOptions(select, 'high');
    expect(select).toHaveValue('high');
    await user.selectOptions(select, 'low');

    expect(select).toHaveValue('low');
    await waitFor(() =>
      expect(patched).toEqual([
        { id: '2', body: { priority: 'high' } },
        { id: '2', body: { priority: 'low' } },
      ]),
    );
  });

  it('removes a line', async () => {
    const user = userEvent.setup();
    render(<OrderListPanel onBookIn={vi.fn()} />);
    await screen.findByRole('region', { name: 'To order' });

    await user.click(within(screen.getByTestId('order-line-2')).getByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(removed).toEqual(['2']));
  });

  it('says how lines get onto an empty list', async () => {
    server.use(http.get('/api/v1/inventory/products/orders', () => HttpResponse.json([])));
    render(<OrderListPanel onBookIn={vi.fn()} />);

    expect(await screen.findByText(/"Reorder" on a spool or in Products puts a line here/)).toBeInTheDocument();
    expect(screen.getByText('Nothing on its way.')).toBeInTheDocument();
    expect(screen.getByText('Nothing to book in.')).toBeInTheDocument();
  });
});
