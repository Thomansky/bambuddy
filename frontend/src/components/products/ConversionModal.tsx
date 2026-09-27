import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, Check, Loader2, WandSparkles, X } from 'lucide-react';
import { api } from '../../api/client';
import type { ProductConversionPlan } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import { formatMoney, formatWeight } from './productUtils';

interface ConversionModalProps {
  onClose: () => void;
  onDone: () => void;
}

// Take over the existing spools (#3165): group them into products and
// variants, preview first, then set the references. Only spools without a
// variant are touched, so it can run again after new spools arrived.
export function ConversionModal({ onClose, onDone }: ConversionModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const currency = getCurrencySymbol(settings?.currency || 'USD');
  const [plan, setPlan] = useState<ProductConversionPlan | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .getProductConversionPlan()
      .then((result) => {
        if (!cancelled) setPlan(result);
      })
      .catch((err) => {
        console.error('ConversionModal.load failed:', err);
        showToast(t('inventory.products.loadFailed'), 'error');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [showToast, t]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const handleRun = async () => {
    setRunning(true);
    try {
      const result = await api.runProductConversion();
      showToast(
        t('inventory.products.conversionDone', { spools: result.spools_to_assign, products: result.new_products }),
        'success',
      );
      onDone();
    } catch (err) {
      console.error('ConversionModal.run failed:', err);
      showToast(t('inventory.products.saveFailed'), 'error');
    } finally {
      setRunning(false);
    }
  };

  const conflictText = (conflict: ProductConversionPlan['conflicts'][number]) => {
    switch (conflict.kind) {
      case 'several_numbers':
        return t('inventory.products.conflictSeveralNumbers', {
          product: conflict.product,
          numbers: conflict.numbers.map((n) => `${n.number} (${n.spools})`).join(', '),
        });
      case 'shared_number':
        return t('inventory.products.conflictSharedNumber', {
          number: conflict.number,
          products: conflict.products.join(', '),
        });
      case 'same_color':
        return t('inventory.products.conflictSameColor', {
          product: conflict.product,
          names: conflict.names.join(' / '),
        });
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="relative w-full max-w-3xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        role="dialog"
        aria-modal="true"
        aria-labelledby="conversion-title"
      >
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <WandSparkles className="w-5 h-5 text-bambu-gray" />
            <h2 id="conversion-title" className="text-lg font-semibold text-white">
              {t('inventory.products.conversionTitle')}
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
          {loading || !plan ? (
            <div className="flex items-center justify-center py-10 text-bambu-gray">
              <Loader2 className="w-5 h-5 animate-spin mr-2" />
              {t('common.loading')}
            </div>
          ) : plan.spools_to_assign === 0 ? (
            <p className="text-sm text-bambu-gray py-6 text-center">{t('inventory.products.conversionNothing')}</p>
          ) : (
            <>
              <p className="text-sm text-bambu-gray">{t('inventory.products.conversionIntro')}</p>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
                {[
                  [plan.spools_to_assign, t('inventory.products.statSpools')],
                  [plan.new_products, t('inventory.products.statProducts')],
                  [plan.new_colors, t('inventory.products.statColors')],
                  [plan.new_variants, t('inventory.products.statVariants')],
                ].map(([value, label]) => (
                  <div key={String(label)} className="p-3 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary">
                    <div className="text-xl font-bold text-white">{value}</div>
                    <div className="text-xs text-bambu-gray">{label}</div>
                  </div>
                ))}
              </div>

              {plan.conflicts.length > 0 && (
                <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 space-y-1">
                  <div className="flex items-center gap-2 text-sm font-medium text-amber-200">
                    <AlertTriangle className="w-4 h-4" />
                    {t('inventory.products.conflictsTitle', { count: plan.conflicts.length })}
                  </div>
                  <ul className="text-xs text-amber-100/80 list-disc pl-6 space-y-0.5">
                    {plan.conflicts.map((conflict, index) => (
                      <li key={index}>{conflictText(conflict)}</li>
                    ))}
                  </ul>
                  <p className="text-xs text-amber-100/60">{t('inventory.products.conflictsHint')}</p>
                </div>
              )}

              <div className="border border-bambu-dark-tertiary rounded-lg divide-y divide-bambu-dark-tertiary">
                {plan.products.map((product) => (
                  <div key={product.label} className="px-4 py-2.5 flex items-start gap-3">
                    <div className="w-14 shrink-0 text-xs font-mono text-bambu-gray pt-0.5">
                      {product.material_number ?? '—'}
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm text-white truncate">{product.label}</span>
                        <span
                          className={`text-[10px] px-1.5 py-0.5 rounded ${
                            product.existing_id === null
                              ? 'bg-bambu-green/20 text-bambu-green'
                              : 'bg-bambu-dark-tertiary text-bambu-gray'
                          }`}
                        >
                          {product.existing_id === null ? t('inventory.products.new') : t('inventory.products.existing')}
                        </span>
                      </div>
                      <div className="flex flex-wrap items-center gap-1.5 mt-1">
                        {product.colors.map((color, index) => (
                          <span
                            key={index}
                            title={`${color.color_name ?? ''} (${color.spool_count})`}
                            className="inline-flex items-center gap-1 text-xs text-bambu-gray"
                          >
                            <FilamentSwatch rgba={color.rgba} effectSize="table" className="w-3.5 h-3.5" />
                            {product.colors.length <= 6 && <span>{color.color_name}</span>}
                          </span>
                        ))}
                        <span className="text-xs text-bambu-gray/60">·</span>
                        {product.sizes.map((size) => (
                          <span key={size.label_weight} className="text-xs text-bambu-gray">
                            {formatWeight(size.label_weight)}
                            {size.price !== null && ` (${formatMoney(size.price, currency)})`}
                          </span>
                        ))}
                      </div>
                    </div>
                    <div className="text-xs text-bambu-gray whitespace-nowrap pt-0.5">
                      {t('inventory.products.spoolCount', { count: product.spool_count })}
                    </div>
                  </div>
                ))}
              </div>
              <p className="text-xs text-bambu-gray">{t('inventory.products.conversionSafe')}</p>
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 px-6 py-4 border-t border-bambu-dark-tertiary">
          <button
            onClick={onClose}
            className="px-3 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary"
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleRun}
            disabled={running || !plan || plan.spools_to_assign === 0}
            className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 disabled:opacity-50 flex items-center gap-1.5"
          >
            {running ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
            {t('inventory.products.conversionRun')}
          </button>
        </div>
      </div>
    </div>
  );
}
