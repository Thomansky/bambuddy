import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, ShoppingCart, X } from 'lucide-react';
import { api } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import { formatMoney, formatSizeLabel, ORDER_QUERY_KEYS } from './productUtils';

interface ReorderLineModalProps {
  /** The colour × size to reorder. */
  variantId: number;
  onClose: () => void;
}

const MAX_QUANTITY = 100;

// Reorder one combination on purpose — from a spool or a cell of the stock
// matrix — whatever its target stock: how many, from whom, and what for. The
// line lands on the reorder list's "to order" column.
export function ReorderLineModal({ variantId, onClose }: ReorderLineModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const refillWord = t('inventory.products.refill');
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const currency = getCurrencySymbol(settings?.currency || 'USD');
  const { data: line, isLoading, isError } = useQuery({
    queryKey: ['reorder-line', variantId],
    queryFn: () => api.getVariantReorderLine(variantId),
    // Stock and what is on order change all the time; never show old ones.
    refetchOnMount: 'always',
    gcTime: 0,
  });
  const [quantity, setQuantity] = useState('1');
  // undefined = not touched yet: the usual supplier once the line has loaded.
  const [supplierId, setSupplierId] = useState<number | null | undefined>(undefined);
  const [reference, setReference] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const chosenSupplier = supplierId === undefined ? (line?.suppliers[0]?.supplier_id ?? null) : supplierId;
  const amount = Math.min(Math.max(parseInt(quantity, 10) || 0, 0), MAX_QUANTITY);
  const color = line ? line.color_name || (line.rgba ? `#${line.rgba.slice(0, 6)}` : '?') : '';

  const handleSubmit = async () => {
    if (!line || amount < 1) return;
    setBusy(true);
    try {
      const result = await api.addProductReorder([
        { variant_id: line.variant_id, quantity: amount, supplier_id: chosenSupplier, reference: reference.trim() || null },
      ]);
      showToast(
        t(result.merged > 0 ? 'inventory.products.reorderOne.merged' : 'inventory.products.reorderOne.added'),
        'success',
      );
      for (const queryKey of ORDER_QUERY_KEYS) queryClient.invalidateQueries({ queryKey });
      onClose();
    } catch (err) {
      console.error('ReorderLineModal.handleSubmit failed:', err);
      showToast(t('inventory.products.reorderOne.failed'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const inputClass =
    'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm focus:border-bambu-green focus:outline-none';

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="relative w-full max-w-lg mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reorder-line-title"
      >
        <div className="flex items-center justify-between gap-4 px-5 py-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 min-w-0">
            <ShoppingCart className="w-5 h-5 text-bambu-gray shrink-0" />
            <h2 id="reorder-line-title" className="text-lg font-semibold text-white truncate">
              {t('inventory.products.reorderOne.title')}
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

        <div className="px-5 py-4 space-y-4">
          {isLoading ? (
            <div className="flex justify-center py-8">
              <Loader2 className="w-6 h-6 text-bambu-green animate-spin" />
            </div>
          ) : isError || !line ? (
            <p className="text-sm text-red-400">{t('inventory.products.reorderOne.loadFailed')}</p>
          ) : (
            <>
              <div className="flex items-center gap-3">
                <FilamentSwatch
                  rgba={line.rgba}
                  extraColors={line.extra_colors}
                  effectType={line.effect_type}
                  effectSize="table"
                  className="w-8 h-8 shrink-0"
                />
                <div className="min-w-0">
                  <div className="text-white font-medium truncate">
                    {line.material_number && <span className="font-mono text-bambu-gray mr-1.5">{line.material_number}</span>}
                    {line.product_label}
                  </div>
                  <div className="text-sm text-bambu-gray">
                    {color} · {formatSizeLabel(line.label_weight, line.refill, refillWord)}
                  </div>
                  <div className="text-xs text-bambu-gray">
                    {t('inventory.products.reorderOne.stock', { stock: line.in_stock, ordered: line.on_order })}
                  </div>
                </div>
              </div>

              <div className="grid grid-cols-[6rem_1fr] gap-3">
                <label className="block">
                  <span className="block text-xs text-bambu-gray mb-1">{t('inventory.products.reorderOne.quantity')}</span>
                  <input
                    className={`${inputClass} text-center`}
                    inputMode="numeric"
                    value={quantity}
                    onChange={(e) => {
                      // What the field shows is what is sent: at most 100.
                      const digits = e.target.value.replace(/[^0-9]/g, '');
                      setQuantity(digits === '' ? '' : String(Math.min(parseInt(digits, 10), MAX_QUANTITY)));
                    }}
                    autoFocus
                  />
                </label>
                <label className="block">
                  <span className="block text-xs text-bambu-gray mb-1">{t('inventory.products.supplier')}</span>
                  <select
                    className={inputClass}
                    value={chosenSupplier ?? ''}
                    onChange={(e) => setSupplierId(e.target.value ? Number(e.target.value) : null)}
                  >
                    {line.suppliers.map((s) => (
                      <option key={s.supplier_id} value={s.supplier_id}>
                        {s.preferred ? `★ ${s.supplier_name}` : s.supplier_name}
                      </option>
                    ))}
                    <option value="">{t('inventory.products.reorder.noSupplier')}</option>
                  </select>
                </label>
              </div>

              <label className="block">
                <span className="block text-xs text-bambu-gray mb-1">{t('inventory.products.reorderOne.reference')}</span>
                <input
                  className={inputClass}
                  value={reference}
                  maxLength={200}
                  placeholder={t('inventory.products.reorderOne.referencePlaceholder')}
                  onChange={(e) => setReference(e.target.value)}
                />
                <span className="block text-xs text-bambu-gray mt-1">
                  {t('inventory.products.reorderOne.referenceHint')}
                </span>
              </label>

              {line.list_price !== null && amount > 0 && (
                <p className="text-sm text-bambu-gray">
                  {t('inventory.products.reorder.total')}: {formatMoney(line.list_price * amount, currency)}
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 py-4 border-t border-bambu-dark-tertiary">
          <button
            onClick={onClose}
            className="px-3 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSubmit}
            disabled={busy || !line || amount < 1}
            className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 flex items-center gap-1.5 disabled:opacity-50"
          >
            {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShoppingCart className="w-4 h-4" />}
            {t('inventory.products.reorderOne.submit')}
          </button>
        </div>
      </div>
    </div>
  );
}
