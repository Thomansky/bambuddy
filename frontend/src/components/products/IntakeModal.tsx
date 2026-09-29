import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Loader2, Minus, PackagePlus, Plus, ScanBarcode, X } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { FilamentProduct } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import {
  colorLabel,
  compareProducts,
  costPerKg,
  findVariant,
  formatMoney,
  formatWeight,
  parsePrice,
  priceText,
  variantParts,
} from './productUtils';

interface IntakeModalProps {
  onClose: () => void;
  /** Open straight on the picker for this product (from the product list). */
  initialProductId?: number | null;
}

type Step = 'scan' | 'pick' | 'confirm' | 'done';

const inputClass =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder-bambu-gray focus:border-bambu-green focus:outline-none';

// Goods in (#3165): scan → the variant is recognised → quantity → spools are
// created, filled in from the master data. An unknown code is taught once by
// picking its variant; the price is the variant's, overridable per delivery.
export function IntakeModal({ onClose, initialProductId = null }: IntakeModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const { data: products = [] } = useQuery({ queryKey: ['filament-products'], queryFn: api.getFilamentProducts });
  const { data: locations = [] } = useQuery({ queryKey: ['intake-locations'], queryFn: api.getLocations });
  const currency = getCurrencySymbol(settings?.currency || 'USD');

  const [step, setStep] = useState<Step>(initialProductId ? 'pick' : 'scan');
  const [code, setCode] = useState('');
  const [pendingCode, setPendingCode] = useState<string | null>(null);
  const [looking, setLooking] = useState(false);
  const [productId, setProductId] = useState<number | null>(initialProductId);
  const [colorId, setColorId] = useState<number | null>(null);
  const [sizeId, setSizeId] = useState<number | null>(null);
  const [variantId, setVariantId] = useState<number | null>(null);
  const [quantity, setQuantity] = useState(1);
  const [price, setPrice] = useState('');
  const [vatIncluded, setVatIncluded] = useState(true);
  const [locationId, setLocationId] = useState<number | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<{ ids: number[]; label: string; settled: number } | null>(null);
  const scanRef = useRef<HTMLInputElement>(null);

  const product: FilamentProduct | undefined = useMemo(
    () => products.find((p) => p.id === productId),
    [products, productId],
  );
  const parts = product && variantId ? variantParts(product, variantId) : null;

  useEffect(() => {
    if (step === 'scan') scanRef.current?.focus();
  }, [step]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const goConfirm = (target: FilamentProduct, id: number) => {
    const found = variantParts(target, id);
    if (!found) return;
    setProductId(target.id);
    setVariantId(id);
    setColorId(found.color.id);
    setSizeId(found.size.id);
    setPrice(priceText(found.variant.effective_price));
    setVatIncluded(found.size.price_vat_included);
    setQuantity(1);
    setStep('confirm');
  };

  const handleScan = async () => {
    const value = code.trim();
    if (!value) return;
    setLooking(true);
    try {
      const found = await api.lookupProductCode(value);
      queryClient.setQueryData<FilamentProduct[]>(['filament-products'], (prev) =>
        prev ? prev.map((p) => (p.id === found.product.id ? found.product : p)) : prev,
      );
      setPendingCode(null);
      goConfirm(found.product, found.variant_id);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        // Unknown code: pick its variant once, and it is known from then on.
        setPendingCode(value);
        setStep('pick');
      } else {
        console.error('IntakeModal.handleScan failed:', err);
        showToast(t('inventory.products.lookupFailed'), 'error');
      }
    } finally {
      setLooking(false);
      setCode('');
    }
  };

  const handlePicked = async () => {
    if (!product || colorId === null || sizeId === null) return;
    const variant = findVariant(product, colorId, sizeId);
    if (!variant) return;
    if (pendingCode) {
      setBusy(true);
      try {
        await api.addVariantCode(variant.id, pendingCode);
        showToast(t('inventory.products.codeLearnt', { code: pendingCode }), 'success');
        setPendingCode(null);
        queryClient.invalidateQueries({ queryKey: ['filament-products'] });
      } catch (err) {
        console.error('IntakeModal.handlePicked failed:', err);
        if (err instanceof ApiError && err.status === 409) {
          showToast(t('inventory.products.codeTaken', { label: String(err.detail?.label ?? '') }), 'error');
        } else {
          showToast(t('inventory.products.saveFailed'), 'error');
        }
        setBusy(false);
        return;
      }
      setBusy(false);
    }
    goConfirm(product, variant.id);
  };

  const handleCreate = async () => {
    if (!parts || !product) return;
    setBusy(true);
    try {
      const result = await api.intakeVariant(parts.variant.id, {
        quantity,
        price_per_spool: parsePrice(price),
        price_vat_included: vatIncluded,
        location_id: locationId,
        note: note.trim() || null,
      });
      setCreated({
        ids: result.spool_ids,
        label: `${product.label} · ${colorLabel(parts.color)} · ${formatWeight(parts.size.label_weight)}`,
        settled: result.orders_settled ?? 0,
      });
      setStep('done');
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      queryClient.invalidateQueries({ queryKey: ['filament-products'] });
      if (result.orders_settled) {
        queryClient.invalidateQueries({ queryKey: ['shopping-list'] });
        queryClient.invalidateQueries({ queryKey: ['filament-products-reorder'] });
      }
    } catch (err) {
      console.error('IntakeModal.handleCreate failed:', err);
      showToast(t('inventory.products.intakeFailed'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const nextScan = () => {
    setCreated(null);
    setVariantId(null);
    setNote('');
    setQuantity(1);
    setStep('scan');
  };

  // Colours with at least one variant, then the sizes that exist in that colour.
  const pickableColors = product ? product.colors.filter((c) => product.variants.some((v) => v.color_id === c.id)) : [];
  const pickableSizes =
    product && colorId !== null
      ? product.sizes.filter((s) => product.variants.some((v) => v.color_id === colorId && v.size_id === s.id))
      : [];

  const perKg = parts ? costPerKg(parsePrice(price), parts.size.label_weight) : null;
  const listPrice = parts ? parts.variant.effective_price : null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="relative w-full max-w-xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[90vh] flex flex-col"
        role="dialog"
        aria-modal="true"
        aria-labelledby="intake-title"
      >
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2">
            <PackagePlus className="w-5 h-5 text-bambu-gray" />
            <h2 id="intake-title" className="text-lg font-semibold text-white">
              {t('inventory.products.intakeTitle')}
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

        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5 space-y-4">
          {step === 'scan' && (
            <>
              <p className="text-sm text-bambu-gray">{t('inventory.products.scanHint')}</p>
              <div className="relative">
                <ScanBarcode className="absolute left-3 top-1/2 -translate-y-1/2 w-5 h-5 text-bambu-gray" />
                <input
                  ref={scanRef}
                  className="w-full pl-11 pr-3 py-3 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-lg font-mono placeholder-bambu-gray/60 focus:border-bambu-green focus:outline-none"
                  value={code}
                  placeholder={t('inventory.products.scanPlaceholder')}
                  onChange={(e) => setCode(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleScan();
                  }}
                  aria-label={t('inventory.products.scanPlaceholder')}
                />
                {looking && <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-5 h-5 animate-spin text-bambu-gray" />}
              </div>
              <div className="flex items-center justify-between gap-2">
                <button
                  onClick={() => {
                    setPendingCode(null);
                    setStep('pick');
                  }}
                  className="text-sm text-bambu-green hover:underline"
                >
                  {t('inventory.products.pickWithoutCode')}
                </button>
                <button
                  onClick={handleScan}
                  disabled={!code.trim() || looking}
                  className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 disabled:opacity-50"
                >
                  {t('inventory.products.lookUp')}
                </button>
              </div>
            </>
          )}

          {step === 'pick' && (
            <>
              {pendingCode ? (
                <div className="p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-sm text-amber-200">
                  {t('inventory.products.unknownCode', { code: pendingCode })}
                </div>
              ) : (
                <p className="text-sm text-bambu-gray">{t('inventory.products.pickHint')}</p>
              )}
              {products.length === 0 ? (
                <p className="text-sm text-bambu-gray">{t('inventory.products.noProductsYet')}</p>
              ) : (
                <>
                  <label className="block">
                    <span className="text-xs text-bambu-gray">{t('inventory.products.product')}</span>
                    <select
                      className={inputClass}
                      value={productId ?? ''}
                      onChange={(e) => {
                        setProductId(e.target.value ? Number(e.target.value) : null);
                        setColorId(null);
                        setSizeId(null);
                      }}
                    >
                      <option value="">{t('inventory.products.choose')}</option>
                      {[...products].sort(compareProducts).map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.material_number ? `${p.material_number} · ` : ''}
                          {p.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  {product && (
                    <div>
                      <span className="text-xs text-bambu-gray">{t('inventory.products.color')}</span>
                      <div className="flex flex-wrap gap-2 mt-1">
                        {pickableColors.map((color) => (
                          <button
                            key={color.id}
                            onClick={() => {
                              setColorId(color.id);
                              const sizesInColor = product.sizes.filter((s) =>
                                product.variants.some((v) => v.color_id === color.id && v.size_id === s.id),
                              );
                              setSizeId(sizesInColor.length === 1 ? sizesInColor[0].id : null);
                            }}
                            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm ${
                              colorId === color.id
                                ? 'border-bambu-green bg-bambu-green/10 text-white'
                                : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                            }`}
                          >
                            <FilamentSwatch
                              rgba={color.rgba}
                              extraColors={color.extra_colors}
                              effectType={color.effect_type}
                              effectSize="table"
                              className="w-4 h-4"
                            />
                            {colorLabel(color)}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {product && colorId !== null && (
                    <div>
                      <span className="text-xs text-bambu-gray">{t('inventory.products.size')}</span>
                      <div className="flex flex-wrap gap-2 mt-1">
                        {pickableSizes.map((size) => (
                          <button
                            key={size.id}
                            onClick={() => setSizeId(size.id)}
                            className={`px-3 py-1.5 rounded-lg border text-sm ${
                              sizeId === size.id
                                ? 'border-bambu-green bg-bambu-green/10 text-white'
                                : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white'
                            }`}
                          >
                            {formatWeight(size.label_weight)}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}
              <div className="flex items-center justify-between gap-2 pt-2">
                <button onClick={() => setStep('scan')} className="text-sm text-bambu-gray hover:text-white">
                  {t('inventory.products.backToScan')}
                </button>
                <button
                  onClick={handlePicked}
                  disabled={!product || colorId === null || sizeId === null || busy}
                  className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 disabled:opacity-50 flex items-center gap-1.5"
                >
                  {busy && <Loader2 className="w-4 h-4 animate-spin" />}
                  {pendingCode ? t('inventory.products.rememberCode') : t('inventory.products.continue')}
                </button>
              </div>
            </>
          )}

          {step === 'confirm' && parts && product && (
            <>
              <div className="flex items-center gap-3 p-3 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary">
                <FilamentSwatch
                  rgba={parts.color.rgba}
                  extraColors={parts.color.extra_colors}
                  effectType={parts.color.effect_type}
                  subtype={product.subtype}
                  effectSize="card"
                  className="w-12 h-12"
                />
                <div className="min-w-0">
                  <div className="text-white font-medium truncate">{product.label}</div>
                  <div className="text-sm text-bambu-gray">
                    {colorLabel(parts.color)} · {formatWeight(parts.size.label_weight)}
                    {product.material_number && (
                      <span className="ml-2 px-1.5 py-0.5 text-xs rounded bg-bambu-dark-tertiary text-white">
                        {t('inventory.products.materialNumberShort', { number: product.material_number })}
                      </span>
                    )}
                  </div>
                  {parts.variant.spool_count > 0 && (
                    <div className="text-xs text-bambu-gray mt-0.5">
                      {t('inventory.products.inStock', { count: parts.variant.spool_count })}
                    </div>
                  )}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="text-xs text-bambu-gray">{t('inventory.quantity')}</span>
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => setQuantity((q) => Math.max(1, q - 1))}
                      className="p-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white"
                      aria-label="-"
                    >
                      <Minus className="w-4 h-4" />
                    </button>
                    <input
                      className={`${inputClass} text-center`}
                      inputMode="numeric"
                      value={quantity}
                      onChange={(e) => {
                        const value = parseInt(e.target.value, 10);
                        setQuantity(Number.isFinite(value) ? Math.min(100, Math.max(1, value)) : 1);
                      }}
                    />
                    <button
                      onClick={() => setQuantity((q) => Math.min(100, q + 1))}
                      className="p-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white"
                      aria-label="+"
                    >
                      <Plus className="w-4 h-4" />
                    </button>
                  </div>
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">{t('inventory.products.pricePerSpool')}</span>
                  <div className="flex items-center gap-1">
                    <span className="text-sm text-bambu-gray">{currency}</span>
                    <input
                      className={inputClass}
                      inputMode="decimal"
                      value={price}
                      onChange={(e) => setPrice(e.target.value)}
                    />
                    <select
                      className="px-2 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm"
                      value={vatIncluded ? 'gross' : 'net'}
                      onChange={(e) => setVatIncluded(e.target.value === 'gross')}
                      aria-label={t('inventory.products.vat')}
                    >
                      <option value="gross">{t('inventory.products.gross')}</option>
                      <option value="net">{t('inventory.products.net')}</option>
                    </select>
                  </div>
                  <span className="text-xs text-bambu-gray">
                    {perKg !== null
                      ? t('inventory.products.becomesPerKg', { price: `${formatMoney(perKg, currency)}/kg` })
                      : t('inventory.products.noPrice')}
                    {listPrice !== null &&
                      parsePrice(price) !== listPrice &&
                      ` · ${t('inventory.products.differsFromList', { price: formatMoney(listPrice, currency) })}`}
                  </span>
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">{t('inventory.products.location')}</span>
                  <select
                    className={inputClass}
                    value={locationId ?? ''}
                    onChange={(e) => setLocationId(e.target.value ? Number(e.target.value) : null)}
                  >
                    <option value="">{t('inventory.products.noLocation')}</option>
                    {locations.map((location) => (
                      <option key={location.id} value={location.id}>
                        {location.name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="text-xs text-bambu-gray">{t('inventory.products.note')}</span>
                  <input className={inputClass} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
                </label>
              </div>

              <div className="flex items-center justify-between gap-2 pt-2">
                <button onClick={() => setStep('scan')} className="text-sm text-bambu-gray hover:text-white">
                  {t('inventory.products.backToScan')}
                </button>
                <button
                  onClick={handleCreate}
                  disabled={busy}
                  className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 disabled:opacity-50 flex items-center gap-1.5"
                >
                  {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <PackagePlus className="w-4 h-4" />}
                  {t('inventory.products.createSpools', { count: quantity })}
                </button>
              </div>
            </>
          )}

          {step === 'done' && created && (
            <>
              <div className="flex items-start gap-3 p-4 rounded-lg bg-bambu-green/10 border border-bambu-green/30">
                <Check className="w-5 h-5 text-bambu-green shrink-0 mt-0.5" />
                <div className="text-sm">
                  <div className="text-white font-medium">
                    {t('inventory.products.spoolsCreated', { count: created.ids.length })}
                  </div>
                  <div className="text-bambu-gray">{created.label}</div>
                  <div className="text-bambu-gray text-xs mt-1">
                    {created.ids.map((id) => `#${id}`).join(', ')}
                  </div>
                  {created.settled > 0 && (
                    <div className="text-bambu-green text-xs mt-1">
                      {t('inventory.products.ordersSettled', { count: created.settled })}
                    </div>
                  )}
                </div>
              </div>
              <div className="flex justify-end gap-2">
                <button
                  onClick={onClose}
                  className="px-3 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary"
                >
                  {t('common.close')}
                </button>
                <button
                  onClick={nextScan}
                  className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 flex items-center gap-1.5"
                >
                  <ScanBarcode className="w-4 h-4" />
                  {t('inventory.products.nextScan')}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
