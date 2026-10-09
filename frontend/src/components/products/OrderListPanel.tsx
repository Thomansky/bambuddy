import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Check, ClipboardList, Loader2, PackageCheck, PackagePlus, Trash2, Truck } from 'lucide-react';
import { api } from '../../api/client';
import type { ProductOrderLine, ProductOrderPriority, ProductOrderStatus, ProductOrderUpdate } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import { formatDateOnly } from '../../utils/date';
import {
  formatMoney,
  formatSizeLabel,
  ORDER_LINES_KEY,
  ORDER_PRIORITIES,
  ORDER_QUERY_KEYS,
  priorityRank,
} from './productUtils';
import { PriorityBadge } from './OrderPriority';

interface OrderListPanelProps {
  /** Book a delivered line in through goods-in. */
  onBookIn: (line: ProductOrderLine) => void;
}

const MAX_QUANTITY = 100;
const COLUMNS: ProductOrderStatus[] = ['pending', 'purchased', 'received'];

const COLUMN_ACCENT: Record<ProductOrderStatus, string> = {
  pending: 'text-amber-400',
  purchased: 'text-blue-400',
  received: 'text-bambu-green',
};

function lineTitle(line: ProductOrderLine): string {
  if (line.variant_id !== null) return line.product_label ?? line.material;
  return [line.brand, line.material, line.subtype].filter(Boolean).join(' ');
}

function lineDetail(line: ProductOrderLine, refillWord: string): string {
  const color = line.color_name || (line.rgba ? `#${line.rgba.slice(0, 6)}` : null);
  const size = line.label_weight !== null ? formatSizeLabel(line.label_weight, line.refill, refillWord) : null;
  return [color, size].filter(Boolean).join(' · ');
}

function lineTotal(lines: ProductOrderLine[]): number | null {
  const priced = lines.filter((line) => line.list_price !== null);
  if (priced.length === 0) return null;
  return priced.reduce((sum, line) => sum + (line.list_price ?? 0) * line.quantity, 0);
}

/** A number field that saves when it loses focus (or on Enter), not on
 *  every keystroke. */
function QuantityField({ value, disabled, onCommit, label }: {
  value: number;
  disabled: boolean;
  onCommit: (quantity: number) => void;
  label: string;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const quantity = parseInt(draft, 10);
    if (!Number.isFinite(quantity) || quantity < 1 || quantity > MAX_QUANTITY) {
      setDraft(String(value));
      return;
    }
    if (quantity !== value) onCommit(quantity);
  };
  return (
    <input
      className="w-12 px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm text-center focus:border-bambu-green focus:outline-none disabled:opacity-60"
      inputMode="numeric"
      aria-label={label}
      value={draft}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value.replace(/[^0-9]/g, ''))}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
        if (e.key === 'Escape') setDraft(String(value));
      }}
    />
  );
}

/** The line's priority. Shows the choice at once and keeps it while the save
 *  runs, so stepping through with the arrow keys moves on from what is shown
 *  rather than from the value still on the server. */
function PriorityField({ value, disabled, onCommit, label }: {
  value: ProductOrderPriority;
  disabled: boolean;
  onCommit: (priority: ProductOrderPriority) => void;
  label: string;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <select
      className="px-1 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-xs text-bambu-gray focus:border-bambu-green focus:outline-none"
      aria-label={label}
      title={label}
      value={draft}
      disabled={disabled}
      onChange={(e) => {
        const priority = e.target.value as ProductOrderPriority;
        setDraft(priority);
        if (priority !== value) onCommit(priority);
      }}
    >
      {ORDER_PRIORITIES.map((priority) => (
        <option key={priority} value={priority}>
          {t(`inventory.products.orders.priority.${priority}`)}
        </option>
      ))}
    </select>
  );
}

/** What the line is for — a job, a customer; saved on blur like the quantity. */
function ReferenceField({ value, disabled, onCommit, placeholder }: {
  value: string | null;
  disabled: boolean;
  onCommit: (reference: string | null) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState(value ?? '');
  useEffect(() => setDraft(value ?? ''), [value]);
  const commit = () => {
    const reference = draft.trim() || null;
    if (reference !== (value ?? null)) onCommit(reference);
  };
  return (
    <input
      className="w-full min-w-0 px-1.5 py-0.5 bg-transparent border border-transparent hover:border-bambu-dark-tertiary rounded text-xs text-white placeholder-bambu-gray focus:border-bambu-green focus:bg-bambu-dark focus:outline-none disabled:hover:border-transparent"
      value={draft}
      maxLength={200}
      disabled={disabled}
      placeholder={placeholder}
      aria-label={placeholder}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
        if (e.key === 'Escape') setDraft(value ?? '');
      }}
    />
  );
}

// The reorder list: what should be ordered, what has been ordered, and what
// has arrived and still has to be booked in — the shopping list's lines in
// three columns. A line moves on with one click; goods-in of its combination
// ticks it off the list.
export function OrderListPanel({ onBookIn }: OrderListPanelProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission, hasAnyPermission } = useAuth();
  const queryClient = useQueryClient();
  const canWrite = hasAnyPermission('inventory:update', 'inventory:forecast_write');
  // Goods-in creates spools, which takes more than changing the list.
  const canBookIn = hasPermission('inventory:update');
  const refillWord = t('inventory.products.refill');
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const currency = getCurrencySymbol(settings?.currency || 'USD');
  const { data: lines = [], isLoading, isError } = useQuery({
    queryKey: ORDER_LINES_KEY,
    queryFn: api.getProductOrders,
  });
  // Bumped when a change fails, so the fields drop what they were showing
  // and start again from the line as saved.
  const [failures, setFailures] = useState(0);

  // Resolves once the list has been fetched again, so a mutation stays
  // pending (and its buttons disabled) until the line has moved.
  const invalidate = () =>
    Promise.all(ORDER_QUERY_KEYS.map((queryKey) => queryClient.invalidateQueries({ queryKey })));

  const updateMutation = useMutation({
    mutationFn: ({ id, changes }: { id: number; changes: ProductOrderUpdate }) => api.updateProductOrder(id, changes),
    onSuccess: () => invalidate(),
    onError: (err) => {
      console.error('OrderListPanel.update failed:', err);
      showToast(t('inventory.products.orders.updateFailed'), 'error');
      setFailures((n) => n + 1);
      invalidate();
    },
  });

  const removeMutation = useMutation({
    mutationFn: (id: number) => api.removeFromShoppingList(id),
    onSuccess: () => invalidate(),
    onError: (err) => {
      console.error('OrderListPanel.remove failed:', err);
      showToast(t('inventory.products.orders.removeFailed'), 'error');
    },
  });

  // Only a move or a removal holds the line's buttons: saving a quantity or a
  // purpose on blur must not swallow the click that caused the blur.
  const busyId =
    updateMutation.isPending && updateMutation.variables?.changes.status !== undefined
      ? updateMutation.variables.id
      : removeMutation.isPending
        ? removeMutation.variables
        : undefined;

  const change = (line: ProductOrderLine, changes: ProductOrderUpdate) => updateMutation.mutate({ id: line.id, changes });

  // Every column most urgent first, then oldest first.
  const byStatus = useMemo(() => {
    const columns: Record<ProductOrderStatus, ProductOrderLine[]> = { pending: [], purchased: [], received: [] };
    const sorted = [...lines].sort((a, b) => priorityRank(a.priority) - priorityRank(b.priority) || a.id - b.id);
    for (const line of sorted) columns[line.status]?.push(line);
    return columns;
  }, [lines]);

  // "To order" grouped by supplier, so one shop's lines go into one order.
  // A shop with something urgent comes first; lines without a supplier last
  // among equally urgent ones.
  const pendingGroups = useMemo(() => {
    const groups = new Map<string, { name: string; noSupplier: boolean; lines: ProductOrderLine[] }>();
    for (const line of byStatus.pending) {
      const key = line.supplier_id !== null ? `s${line.supplier_id}` : 'none';
      if (!groups.has(key)) {
        groups.set(key, {
          name: line.supplier_name ?? t('inventory.products.reorder.noSupplier'),
          noSupplier: line.supplier_id === null,
          lines: [],
        });
      }
      groups.get(key)?.lines.push(line);
    }
    const urgency = (group: { lines: ProductOrderLine[] }) =>
      Math.min(...group.lines.map((line) => priorityRank(line.priority)));
    return [...groups.values()].sort(
      (a, b) =>
        urgency(a) - urgency(b) ||
        (a.noSupplier === b.noSupplier ? a.name.localeCompare(b.name) : a.noSupplier ? 1 : -1),
    );
  }, [byStatus.pending, t]);

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
      </div>
    );
  }
  if (isError) {
    return <p className="py-8 text-center text-sm text-red-400">{t('inventory.products.orders.loadFailed')}</p>;
  }

  const renderLine = (line: ProductOrderLine) => {
    const busy = busyId === line.id;
    const detail = lineDetail(line, refillWord);
    const dateKey =
      line.status === 'received' ? 'arrivedOn' : line.status === 'purchased' ? 'orderedOn' : 'addedOn';
    const date = line.status === 'received' ? line.received_at : line.status === 'purchased' ? line.purchased_at : line.added_at;
    const pendingSupplierChoice = line.status === 'pending' && line.variant_id !== null && canWrite;
    return (
      <li
        key={line.id}
        className={`rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3 space-y-2 ${
          line.priority === 'high' ? 'border-l-4 border-l-red-500' : ''
        }`}
        data-testid={`order-line-${line.id}`}
      >
        <div className="flex items-start gap-2.5">
          {line.variant_id !== null ? (
            <FilamentSwatch
              rgba={line.rgba}
              extraColors={line.extra_colors}
              effectType={line.effect_type}
              effectSize="table"
              className="w-6 h-6 shrink-0 mt-0.5"
            />
          ) : (
            <ClipboardList className="w-5 h-5 shrink-0 mt-0.5 text-bambu-gray" aria-label={t('inventory.products.orders.plainLine')} />
          )}
          <div className="min-w-0 flex-1">
            <div className="text-sm text-white font-medium truncate" title={lineTitle(line)}>
              {line.material_number && <span className="font-mono text-bambu-gray mr-1.5">{line.material_number}</span>}
              {lineTitle(line)}
            </div>
            {(detail || line.priority !== 'normal') && (
              <div className="flex items-center gap-1.5 min-w-0">
                <PriorityBadge priority={line.priority} />
                {detail && <span className="text-xs text-bambu-gray truncate">{detail}</span>}
              </div>
            )}
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {canWrite ? (
              <QuantityField
                key={`quantity-${failures}`}
                value={line.quantity}
                disabled={busy}
                label={t('inventory.products.orders.quantityLabel')}
                onCommit={(quantity) => change(line, { quantity })}
              />
            ) : (
              <span className="text-sm text-white font-semibold">{line.quantity}</span>
            )}
            <span className="text-xs text-bambu-gray">{t('inventory.products.orders.spoolsUnit', { count: line.quantity })}</span>
          </div>
        </div>

        {canWrite ? (
          <ReferenceField
            key={`reference-${failures}`}
            value={line.reference}
            disabled={busy}
            placeholder={t('inventory.products.orders.referencePlaceholder')}
            onCommit={(reference) => change(line, { reference })}
          />
        ) : (
          line.reference && <div className="text-xs text-white truncate">{line.reference}</div>
        )}

        <div className="flex items-center gap-2 text-xs text-bambu-gray">
          {pendingSupplierChoice ? (
            <select
              className="min-w-0 max-w-[60%] px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-xs text-white focus:border-bambu-green focus:outline-none"
              aria-label={t('inventory.products.supplier')}
              value={line.supplier_id ?? ''}
              disabled={busy}
              onChange={(e) => change(line, { supplier_id: e.target.value ? Number(e.target.value) : null })}
            >
              {line.suppliers.map((s) => (
                <option key={s.supplier_id} value={s.supplier_id}>
                  {s.preferred ? `★ ${s.supplier_name}` : s.supplier_name}
                </option>
              ))}
              {line.supplier_id !== null && !line.suppliers.some((s) => s.supplier_id === line.supplier_id) && (
                <option value={line.supplier_id}>{line.supplier_name}</option>
              )}
              <option value="">{t('inventory.products.reorder.noSupplier')}</option>
            </select>
          ) : (
            line.status !== 'pending' && line.supplier_name && <span className="truncate">{line.supplier_name}</span>
          )}
          {line.list_price !== null && (
            <span className="whitespace-nowrap">{formatMoney(line.list_price * line.quantity, currency)}</span>
          )}
          {date && (
            <span className="ml-auto whitespace-nowrap">
              {t(`inventory.products.orders.${dateKey}`, { date: formatDateOnly(date) })}
            </span>
          )}
        </div>

        {canWrite && (
          <div className="flex items-center gap-1.5 pt-1">
            {line.status !== 'pending' && (
              <button
                onClick={() => change(line, { status: line.status === 'received' ? 'purchased' : 'pending' })}
                disabled={busy}
                className="px-2 py-1 rounded text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary flex items-center gap-1 disabled:opacity-50"
                title={t('inventory.products.orders.backTitle')}
              >
                <ArrowLeft className="w-3.5 h-3.5" />
                {t('inventory.products.orders.back')}
              </button>
            )}
            <button
              onClick={() => removeMutation.mutate(line.id)}
              disabled={busy}
              className="p-1 rounded text-bambu-gray hover:text-red-400 hover:bg-bambu-dark-tertiary disabled:opacity-50"
              title={t('inventory.products.orders.remove')}
              aria-label={t('inventory.products.orders.remove')}
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
            <PriorityField
              key={`priority-${failures}`}
              value={line.priority}
              disabled={busy}
              label={t('inventory.products.orders.priority.label')}
              onCommit={(priority) => change(line, { priority })}
            />
            <div className="ml-auto">
              {line.status === 'pending' && (
                <button
                  onClick={() => change(line, { status: 'purchased' })}
                  disabled={busy}
                  className="px-2.5 py-1 rounded bg-blue-500/20 text-blue-300 hover:bg-blue-500/30 text-xs flex items-center gap-1 disabled:opacity-50"
                >
                  {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Truck className="w-3.5 h-3.5" />}
                  {t('inventory.products.orders.markOrdered')}
                </button>
              )}
              {line.status === 'purchased' && line.variant_id === null && (
                <span className="text-xs text-bambu-gray" title={t('inventory.products.orders.plainReceiveHint')}>
                  {t('inventory.products.orders.plainReceive')}
                </span>
              )}
              {line.status === 'purchased' && line.variant_id !== null && (
                <button
                  onClick={() => change(line, { status: 'received' })}
                  disabled={busy}
                  className="px-2.5 py-1 rounded bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 text-xs flex items-center gap-1 disabled:opacity-50"
                >
                  {busy ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <PackageCheck className="w-3.5 h-3.5" />}
                  {t('inventory.products.orders.markArrived')}
                </button>
              )}
              {line.status === 'received' && line.variant_id !== null && line.product_id !== null && canBookIn && (
                <button
                  onClick={() => onBookIn(line)}
                  disabled={busy}
                  className="px-2.5 py-1 rounded bg-bambu-green text-white hover:bg-bambu-green/80 text-xs flex items-center gap-1 disabled:opacity-50"
                >
                  <PackagePlus className="w-3.5 h-3.5" />
                  {t('inventory.products.orders.bookIn')}
                </button>
              )}
              {line.status === 'received' && (line.variant_id === null || line.product_id === null) && (
                <button
                  onClick={() => removeMutation.mutate(line.id)}
                  disabled={busy}
                  className="px-2.5 py-1 rounded bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 text-xs flex items-center gap-1 disabled:opacity-50"
                  title={t('inventory.products.orders.doneTitle')}
                >
                  <Check className="w-3.5 h-3.5" />
                  {t('inventory.products.orders.done')}
                </button>
              )}
            </div>
          </div>
        )}
      </li>
    );
  };

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-white">{t('inventory.products.orders.title')}</h2>
        <p className="text-sm text-bambu-gray">{t('inventory.products.orders.hint')}</p>
      </div>
      <div className="grid gap-4 lg:grid-cols-3">
        {COLUMNS.map((status) => {
          const columnLines = byStatus[status];
          const spools = columnLines.reduce((sum, line) => sum + line.quantity, 0);
          return (
            <section
              key={status}
              className="rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3 flex flex-col min-h-[8rem]"
              aria-labelledby={`orders-column-${status}`}
              data-testid={`orders-column-${status}`}
            >
              <div className="flex items-baseline justify-between gap-2 mb-3">
                <h3 id={`orders-column-${status}`} className={`text-sm font-semibold ${COLUMN_ACCENT[status]}`}>
                  {t(`inventory.products.orders.columns.${status}`)}
                </h3>
                <span className="text-xs text-bambu-gray">
                  {t('inventory.products.orders.lineCount', { count: columnLines.length })}
                  {columnLines.length > 0 && ` · ${t('inventory.products.orders.spools', { count: spools })}`}
                </span>
              </div>
              {columnLines.length === 0 ? (
                <p className="text-xs text-bambu-gray py-4 text-center">
                  {t(`inventory.products.orders.empty.${status}`)}
                </p>
              ) : status === 'pending' ? (
                <div className="space-y-3">
                  {pendingGroups.map((group) => {
                    const total = lineTotal(group.lines);
                    return (
                      <div key={group.name + String(group.noSupplier)}>
                        <div className="flex items-baseline justify-between gap-2 mb-1.5 text-xs">
                          <h4 className="text-white font-medium truncate">{group.name}</h4>
                          {total !== null && (
                            <span className="text-bambu-gray whitespace-nowrap">
                              {t('inventory.products.orders.total')}: {formatMoney(total, currency)}
                            </span>
                          )}
                        </div>
                        <ul className="space-y-2">{group.lines.map(renderLine)}</ul>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <ul className="space-y-2">{columnLines.map(renderLine)}</ul>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
