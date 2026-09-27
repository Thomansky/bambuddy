import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Loader2, ShoppingCart, X } from 'lucide-react';
import { api } from '../../api/client';
import type { ProductReorderLine } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import { formatMoney, formatWeight, reorderPrice } from './productUtils';

interface ReorderModalProps {
  onClose: () => void;
  onDone: () => void;
}

interface LineChoice {
  selected: boolean;
  quantity: string;
  supplierId: number | null;
}

const MAX_QUANTITY = 100;

/** Every line starts ticked, with its shortfall, at the usual supplier. */
function defaultChoice(line: ProductReorderLine): LineChoice {
  return { selected: true, quantity: String(line.shortfall), supplierId: line.suppliers[0]?.supplier_id ?? null };
}

function quantityOf(choice: LineChoice): number {
  const value = parseInt(choice.quantity, 10);
  return Number.isFinite(value) ? Math.min(Math.max(value, 0), MAX_QUANTITY) : 0;
}

function lineLabel(line: ProductReorderLine): string {
  const color = line.color_name || (line.rgba ? `#${line.rgba.slice(0, 6)}` : '?');
  return `${line.product_label} · ${color} · ${formatWeight(line.label_weight)}`;
}

// Reorder (#3165): every colour × size below its target stock, less what the
// shopping list already waits for, grouped by the supplier it is bought from.
// The ticked lines go onto the shopping list tied to their combination, so
// goods-in ticks them off again.
export function ReorderModal({ onClose, onDone }: ReorderModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const currency = getCurrencySymbol(settings?.currency || 'USD');
  const { data: lines = [], isLoading } = useQuery({
    queryKey: ['filament-products-reorder'],
    queryFn: api.getProductReorder,
  });
  const [choices, setChoices] = useState<Record<number, LineChoice>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const resolved = useMemo(
    () => new Map(lines.map((line) => [line.variant_id, choices[line.variant_id] ?? defaultChoice(line)])),
    [lines, choices],
  );

  const update = (line: ProductReorderLine, patch: Partial<LineChoice>) =>
    setChoices((prev) => ({ ...prev, [line.variant_id]: { ...(resolved.get(line.variant_id) ?? defaultChoice(line)), ...patch } }));

  // Grouped by the supplier chosen for each line; lines without one last.
  const groups = useMemo(() => {
    const byKey = new Map<string, { supplierId: number | null; name: string; lines: ProductReorderLine[] }>();
    for (const line of lines) {
      const supplierId = resolved.get(line.variant_id)?.supplierId ?? null;
      const supplier = line.suppliers.find((s) => s.supplier_id === supplierId);
      const key = supplier ? `s${supplier.supplier_id}` : 'none';
      if (!byKey.has(key)) {
        byKey.set(key, {
          supplierId: supplier?.supplier_id ?? null,
          name: supplier?.supplier_name ?? t('inventory.products.reorder.noSupplier'),
          lines: [],
        });
      }
      byKey.get(key)?.lines.push(line);
    }
    return [...byKey.values()].sort((a, b) =>
      a.supplierId === null ? 1 : b.supplierId === null ? -1 : a.name.localeCompare(b.name),
    );
  }, [lines, resolved, t]);

  const chosen = lines.filter((line) => {
    const choice = resolved.get(line.variant_id);
    return choice?.selected && quantityOf(choice) > 0;
  });

  const totalOf = (subset: ProductReorderLine[]) =>
    subset.reduce((sum, line) => {
      const choice = resolved.get(line.variant_id);
      if (!choice?.selected) return sum;
      const price = reorderPrice(line, choice.supplierId);
      return price === null ? sum : sum + price * quantityOf(choice);
    }, 0);

  const handleSubmit = async () => {
    const items = chosen.map((line) => {
      const choice = resolved.get(line.variant_id) ?? defaultChoice(line);
      return { variant_id: line.variant_id, quantity: quantityOf(choice), supplier_id: choice.supplierId };
    });
    if (items.length === 0) return;
    setBusy(true);
    try {
      const result = await api.addProductReorder(items);
      showToast(t('inventory.products.reorder.added', { count: result.added + result.merged }), 'success');
      queryClient.invalidateQueries({ queryKey: ['shopping-list'] });
      queryClient.invalidateQueries({ queryKey: ['filament-products'] });
      queryClient.invalidateQueries({ queryKey: ['filament-products-reorder'] });
      onDone();
    } catch (err) {
      console.error('ReorderModal.handleSubmit failed:', err);
      showToast(t('inventory.products.reorder.failed'), 'error');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="relative w-full max-w-5xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[92vh] flex flex-col"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reorder-title"
      >
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 min-w-0">
            <ShoppingCart className="w-5 h-5 text-bambu-gray shrink-0" />
            <h2 id="reorder-title" className="text-lg font-semibold text-white truncate">
              {t('inventory.products.reorder.title')}
            </h2>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-4 space-y-4">
          {isLoading ? (
            <div className="flex justify-center py-12">
              <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
            </div>
          ) : lines.length === 0 ? (
            <div className="text-center py-10 space-y-2">
              <Check className="w-10 h-10 text-bambu-green mx-auto" />
              <p className="text-white font-medium">{t('inventory.products.reorder.empty')}</p>
              <p className="text-sm text-bambu-gray max-w-lg mx-auto">{t('inventory.products.reorder.emptyHint')}</p>
            </div>
          ) : (
            <>
              <p className="text-xs text-bambu-gray">{t('inventory.products.reorder.hint')}</p>
              {groups.map((group) => (
                <section key={group.supplierId ?? 'none'} className="border border-bambu-dark-tertiary rounded-lg overflow-hidden">
                  <div className="flex items-center justify-between gap-2 px-3 py-2 bg-bambu-dark">
                    <h3 className="text-sm font-medium text-white">{group.name}</h3>
                    <span className="text-xs text-bambu-gray">
                      {t('inventory.products.reorder.total')}: {formatMoney(totalOf(group.lines), currency)}
                    </span>
                  </div>
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-xs text-bambu-gray">
                          <th className="w-8" />
                          <th className="px-2 py-1.5 text-left font-medium">{t('inventory.products.product')}</th>
                          <th className="px-2 py-1.5 text-left font-medium">{t('inventory.products.reorder.quantity')}</th>
                          <th className="px-2 py-1.5 text-left font-medium">{t('inventory.products.supplier')}</th>
                          <th className="px-2 py-1.5 text-left font-medium hidden md:table-cell">
                            {t('inventory.products.articleNumber')}
                          </th>
                          <th className="px-2 py-1.5 text-right font-medium">{t('inventory.products.reorder.unitPrice')}</th>
                          <th className="px-2 py-1.5 text-right font-medium hidden sm:table-cell">
                            {t('inventory.products.reorder.total')}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {group.lines.map((line) => {
                          const choice = resolved.get(line.variant_id) ?? defaultChoice(line);
                          const supplier = line.suppliers.find((s) => s.supplier_id === choice.supplierId);
                          const price = reorderPrice(line, choice.supplierId);
                          const quantity = quantityOf(choice);
                          return (
                            <tr
                              key={line.variant_id}
                              className={`border-t border-bambu-dark-tertiary ${choice.selected ? '' : 'opacity-50'}`}
                            >
                              <td className="pl-3 py-1.5">
                                <input
                                  type="checkbox"
                                  checked={choice.selected}
                                  onChange={() => update(line, { selected: !choice.selected })}
                                  className="w-4 h-4 accent-bambu-green"
                                  aria-label={lineLabel(line)}
                                />
                              </td>
                              <td className="px-2 py-1.5">
                                <div className="flex items-center gap-2">
                                  <FilamentSwatch
                                    rgba={line.rgba}
                                    extraColors={line.extra_colors}
                                    effectType={line.effect_type}
                                    effectSize="table"
                                    className="w-4 h-4 shrink-0"
                                  />
                                  <div className="min-w-0">
                                    <div className="text-white truncate">
                                      {line.material_number && (
                                        <span className="font-mono text-bambu-gray mr-1.5">{line.material_number}</span>
                                      )}
                                      {lineLabel(line)}
                                    </div>
                                    <div className="text-xs text-bambu-gray">
                                      {t('inventory.products.reorder.status', {
                                        target: line.min_stock,
                                        stock: line.in_stock,
                                        ordered: line.on_order,
                                      })}
                                    </div>
                                  </div>
                                </div>
                              </td>
                              <td className="px-2 py-1.5">
                                <input
                                  className="w-16 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm text-center focus:border-bambu-green focus:outline-none"
                                  inputMode="numeric"
                                  value={choice.quantity}
                                  onChange={(e) => update(line, { quantity: e.target.value.replace(/[^0-9]/g, '') })}
                                  aria-label={`${t('inventory.products.reorder.quantity')} ${lineLabel(line)}`}
                                />
                              </td>
                              <td className="px-2 py-1.5 min-w-[9rem]">
                                <select
                                  className="w-full px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm focus:border-bambu-green focus:outline-none"
                                  value={choice.supplierId ?? ''}
                                  onChange={(e) =>
                                    update(line, { supplierId: e.target.value ? Number(e.target.value) : null })
                                  }
                                  aria-label={`${t('inventory.products.supplier')} ${lineLabel(line)}`}
                                >
                                  {line.suppliers.map((s) => (
                                    <option key={s.supplier_id} value={s.supplier_id}>
                                      {s.preferred ? `★ ${s.supplier_name}` : s.supplier_name}
                                    </option>
                                  ))}
                                  <option value="">{t('inventory.products.reorder.noSupplier')}</option>
                                </select>
                              </td>
                              <td className="px-2 py-1.5 font-mono text-xs text-bambu-gray hidden md:table-cell">
                                {supplier?.article_number ?? '–'}
                              </td>
                              <td className="px-2 py-1.5 text-right text-bambu-gray whitespace-nowrap">
                                {formatMoney(price, currency)}
                              </td>
                              <td className="px-2 py-1.5 text-right text-white whitespace-nowrap hidden sm:table-cell">
                                {price === null || quantity === 0 ? '–' : formatMoney(price * quantity, currency)}
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                </section>
              ))}
            </>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 px-6 py-4 border-t border-bambu-dark-tertiary">
          <span className="text-sm text-bambu-gray">
            {lines.length > 0 && `${t('inventory.products.reorder.total')}: ${formatMoney(totalOf(lines), currency)}`}
          </span>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-3 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary"
            >
              {t('common.cancel')}
            </button>
            <button
              onClick={handleSubmit}
              disabled={busy || chosen.length === 0}
              className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 flex items-center gap-1.5 disabled:opacity-50"
            >
              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShoppingCart className="w-4 h-4" />}
              {t('inventory.products.reorder.addToList', { count: chosen.length })}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
