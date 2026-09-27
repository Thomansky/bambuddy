import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { Barcode, Boxes, Check, Loader2, Plus, Star, Store, Trash2, X } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { FilamentProduct, FilamentProductInput, FilamentVariant } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { ConfirmModal } from '../ConfirmModal';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import { costPerKg, formatMoney, formatStock, formatWeight, parsePrice, priceText } from './productUtils';

interface ProductEditorModalProps {
  /** null = a new product. */
  product: FilamentProduct | null;
  onClose: () => void;
  onSaved: () => void;
}

interface SizeRow {
  id: number | null;
  key: string;
  label_weight: string;
  core_weight: string;
  price: string;
  price_vat_included: boolean;
}

interface SupplierRow {
  key: string;
  supplier_id: number | null;
  article_number: string;
  preferred: boolean;
  /** Price text per size key. */
  prices: Record<string, string>;
}

interface ColorRow {
  id: number | null;
  key: string;
  color_name: string;
  /** Six hex digits without '#', or null for "no colour set". */
  hex: string | null;
  alpha: string;
}

const QUICK_SIZES = [250, 500, 750, 1000, 2000, 3000, 5000, 10000];
const inputClass =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder-bambu-gray focus:border-bambu-green focus:outline-none';
const smallInputClass =
  'w-full px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm placeholder-bambu-gray/60 focus:border-bambu-green focus:outline-none';

let keySequence = 0;
const newKey = (prefix: string) => `${prefix}new${++keySequence}`;
const cellKey = (colorKey: string, sizeKey: string) => `${colorKey}|${sizeKey}`;

function toInt(text: string): number | null {
  const value = parseInt(text.trim(), 10);
  return Number.isFinite(value) ? value : null;
}

// Product master data (#3165): one product, its colours and sizes, and the
// matrix of combinations that exist. Saving sends the whole document; the
// backend writes master-data changes through to the spools (never the price).
export function ProductEditorModal({ product, onClose, onSaved }: ProductEditorModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const { data: allSuppliers = [] } = useQuery({ queryKey: ['suppliers'], queryFn: api.getSuppliers });
  const currency = getCurrencySymbol(settings?.currency || 'USD');

  const [brand, setBrand] = useState(product?.brand ?? '');
  const [material, setMaterial] = useState(product?.material ?? '');
  const [subtype, setSubtype] = useState(product?.subtype ?? '');
  const [materialNumber, setMaterialNumber] = useState(product?.material_number ?? '');
  const [slicerFilament, setSlicerFilament] = useState(product?.slicer_filament ?? '');
  const [slicerFilamentName, setSlicerFilamentName] = useState(product?.slicer_filament_name ?? '');
  const [tempMin, setTempMin] = useState(product?.nozzle_temp_min?.toString() ?? '');
  const [tempMax, setTempMax] = useState(product?.nozzle_temp_max?.toString() ?? '');
  const [note, setNote] = useState(product?.note ?? '');
  const [sizes, setSizes] = useState<SizeRow[]>(
    () =>
      product?.sizes.map((s) => ({
        id: s.id,
        key: `s${s.id}`,
        label_weight: String(s.label_weight),
        core_weight: String(s.core_weight),
        price: priceText(s.price),
        price_vat_included: s.price_vat_included,
      })) ?? [],
  );
  const [colors, setColors] = useState<ColorRow[]>(
    () =>
      product?.colors.map((c) => ({
        id: c.id,
        key: `c${c.id}`,
        color_name: c.color_name ?? '',
        hex: c.rgba ? c.rgba.slice(0, 6) : null,
        alpha: c.rgba && c.rgba.length === 8 ? c.rgba.slice(6, 8) : 'FF',
      })) ?? [],
  );
  // Ticked cells of the matrix, with an optional price for just that combination.
  const [cells, setCells] = useState<Map<string, string>>(
    () =>
      new Map(product?.variants.map((v) => [cellKey(`c${v.color_id}`, `s${v.size_id}`), priceText(v.price_override)]) ?? []),
  );
  const [codes, setCodes] = useState(() =>
    (product?.variants ?? []).flatMap((v) => v.codes.map((code) => ({ ...code, variantId: v.id }))),
  );
  const [supplierRows, setSupplierRows] = useState<SupplierRow[]>(
    () =>
      product?.suppliers.map((row) => ({
        key: newKey('p'),
        supplier_id: row.supplier_id,
        article_number: row.article_number ?? '',
        preferred: row.preferred,
        prices: Object.fromEntries(row.prices.map((p) => [`s${p.size_id}`, priceText(p.price)])),
      })) ?? [],
  );
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Existing variants by cell, for stock counts and the removal guard.
  const existingByCell = useMemo(() => {
    const map = new Map<string, FilamentVariant>();
    for (const variant of product?.variants ?? []) map.set(cellKey(`c${variant.color_id}`, `s${variant.size_id}`), variant);
    return map;
  }, [product]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !confirmDelete) onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose, confirmDelete]);

  const spoolsInCell = (key: string) => existingByCell.get(key)?.spool_count ?? 0;
  const spoolsInRow = (colorKey: string) =>
    sizes.reduce((sum, size) => sum + spoolsInCell(cellKey(colorKey, size.key)), 0);
  const spoolsInColumn = (sizeKey: string) =>
    colors.reduce((sum, color) => sum + spoolsInCell(cellKey(color.key, sizeKey)), 0);

  const addSize = (grams?: number) => {
    if (grams && sizes.some((s) => toInt(s.label_weight) === grams)) return;
    const key = newKey('s');
    setSizes((prev) => [
      ...prev,
      {
        id: null,
        key,
        label_weight: grams ? String(grams) : '',
        core_weight: grams && grams <= 1000 ? '250' : '',
        price: '',
        price_vat_included: prev[0]?.price_vat_included ?? true,
      },
    ]);
    // A new size is usually sold in every colour — tick the column.
    setCells((prev) => {
      const next = new Map(prev);
      for (const color of colors) next.set(cellKey(color.key, key), '');
      return next;
    });
  };

  const addColor = () => {
    const key = newKey('c');
    setColors((prev) => [...prev, { id: null, key, color_name: '', hex: '808080', alpha: 'FF' }]);
    setCells((prev) => {
      const next = new Map(prev);
      for (const size of sizes) next.set(cellKey(key, size.key), '');
      return next;
    });
  };

  const removeSize = (key: string) => {
    setSizes((prev) => prev.filter((s) => s.key !== key));
    setCells((prev) => new Map([...prev].filter(([k]) => !k.endsWith(`|${key}`))));
  };

  const removeColor = (key: string) => {
    setColors((prev) => prev.filter((c) => c.key !== key));
    setCells((prev) => new Map([...prev].filter(([k]) => !k.startsWith(`${key}|`))));
  };

  const toggleCell = (key: string) => {
    setCells((prev) => {
      const next = new Map(prev);
      if (next.has(key)) next.delete(key);
      else next.set(key, '');
      return next;
    });
  };

  const setAllCells = (on: boolean) => {
    setCells((prev) => {
      const next = new Map<string, string>();
      for (const color of colors) {
        for (const size of sizes) {
          const key = cellKey(color.key, size.key);
          // Cells holding spools stay ticked either way.
          if (on || spoolsInCell(key) > 0) next.set(key, prev.get(key) ?? '');
        }
      }
      return next;
    });
  };

  const updateSize = (key: string, patch: Partial<SizeRow>) =>
    setSizes((prev) => prev.map((s) => (s.key === key ? { ...s, ...patch } : s)));
  const updateColor = (key: string, patch: Partial<ColorRow>) =>
    setColors((prev) => prev.map((c) => (c.key === key ? { ...c, ...patch } : c)));

  const removeCode = async (codeId: number) => {
    try {
      await api.deleteVariantCode(codeId);
      setCodes((prev) => prev.filter((c) => c.id !== codeId));
    } catch (err) {
      console.error('ProductEditorModal.removeCode failed:', err);
      showToast(t('inventory.products.saveFailed'), 'error');
    }
  };

  const handleSave = async () => {
    if (!material.trim()) {
      showToast(t('inventory.products.materialRequired'), 'error');
      return;
    }
    if (sizes.some((s) => !toInt(s.label_weight) || (toInt(s.label_weight) ?? 0) <= 0)) {
      showToast(t('inventory.products.sizeWeightRequired'), 'error');
      return;
    }
    const sizeKeys = new Set(sizes.map((s) => s.key));
    const colorKeys = new Set(colors.map((c) => c.key));
    const document: FilamentProductInput = {
      brand: brand.trim() || null,
      material: material.trim(),
      subtype: subtype.trim() || null,
      material_number: materialNumber.trim() || null,
      slicer_filament: slicerFilament.trim() || null,
      slicer_filament_name: slicerFilamentName.trim() || null,
      nozzle_temp_min: toInt(tempMin),
      nozzle_temp_max: toInt(tempMax),
      note: note.trim() || null,
      sizes: sizes.map((s) => ({
        id: s.id,
        key: s.key,
        label_weight: toInt(s.label_weight) ?? 0,
        core_weight: toInt(s.core_weight) ?? 0,
        price: parsePrice(s.price),
        price_vat_included: s.price_vat_included,
      })),
      colors: colors.map((c) => ({
        id: c.id,
        key: c.key,
        color_name: c.color_name.trim() || null,
        rgba: c.hex ? `${c.hex}${c.alpha}`.toUpperCase() : null,
      })),
      variants: [...cells.entries()]
        .map(([key, price]) => {
          const [colorKey, sizeKey] = key.split('|');
          return { color_key: colorKey, size_key: sizeKey, price_override: parsePrice(price) };
        })
        .filter((v) => colorKeys.has(v.color_key) && sizeKeys.has(v.size_key)),
      suppliers: supplierRows
        .filter((row) => row.supplier_id !== null)
        .map((row) => ({
          supplier_id: row.supplier_id as number,
          article_number: row.article_number.trim() || null,
          preferred: row.preferred,
          prices: Object.fromEntries(
            Object.entries(row.prices)
              .filter(([key]) => sizeKeys.has(key))
              .map(([key, text]) => [key, parsePrice(text)]),
          ),
        })),
    };
    setSaving(true);
    try {
      if (product) {
        const result = await api.updateFilamentProduct(product.id, document);
        showToast(
          result.spools_updated > 0
            ? t('inventory.products.savedWithSpools', { count: result.spools_updated })
            : t('inventory.products.saved'),
          'success',
        );
      } else {
        await api.createFilamentProduct(document);
        showToast(t('inventory.products.created'), 'success');
      }
      onSaved();
    } catch (err) {
      console.error('ProductEditorModal.handleSave failed:', err);
      if (err instanceof ApiError && err.status === 409) {
        showToast(t('inventory.products.variantInUse', { count: Number(err.detail?.spool_count ?? 0) }), 'error');
      } else if (err instanceof ApiError && err.status === 400) {
        showToast(err.message, 'error');
      } else {
        showToast(t('inventory.products.saveFailed'), 'error');
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!product) return;
    try {
      const result = await api.deleteFilamentProduct(product.id);
      showToast(t('inventory.products.deleted', { count: result.spools_unlinked }), 'success');
      onSaved();
    } catch (err) {
      console.error('ProductEditorModal.handleDelete failed:', err);
      showToast(t('inventory.products.saveFailed'), 'error');
    } finally {
      setConfirmDelete(false);
    }
  };

  const variantName = (variantId: number) => {
    const variant = product?.variants.find((v) => v.id === variantId);
    if (!variant) return '?';
    const color = colors.find((c) => c.id === variant.color_id);
    const size = sizes.find((s) => s.id === variant.size_id);
    return `${color?.color_name || (color?.hex ? `#${color.hex}` : '?')} · ${size ? formatWeight(toInt(size.label_weight) ?? 0) : '?'}`;
  };

  const tickedCount = [...cells.keys()].filter((key) => {
    const [colorKey, sizeKey] = key.split('|');
    return colors.some((c) => c.key === colorKey) && sizes.some((s) => s.key === sizeKey);
  }).length;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="relative w-full max-w-5xl mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[92vh] flex flex-col"
        role="dialog"
        aria-modal="true"
        aria-labelledby="product-editor-title"
      >
        <div className="flex items-center justify-between gap-4 px-6 py-4 border-b border-bambu-dark-tertiary">
          <div className="flex items-center gap-2 min-w-0">
            <Boxes className="w-5 h-5 text-bambu-gray shrink-0" />
            <h2 id="product-editor-title" className="text-lg font-semibold text-white truncate">
              {product ? product.label : t('inventory.products.newProduct')}
            </h2>
            {product && product.spool_count > 0 && (
              <span className="text-sm text-bambu-gray shrink-0">
                {t('inventory.products.spoolCount', { count: product.spool_count })}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white transition-colors"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-4 space-y-6">
          {/* Master data */}
          <section className="space-y-3">
            <h3 className="text-sm font-medium text-white">{t('inventory.products.masterData')}</h3>
            <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.brand')}</span>
                <input className={inputClass} value={brand} maxLength={100} onChange={(e) => setBrand(e.target.value)} />
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.material')} *</span>
                <input
                  className={inputClass}
                  value={material}
                  maxLength={50}
                  onChange={(e) => setMaterial(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.products.subtype')}</span>
                <input className={inputClass} value={subtype} maxLength={50} onChange={(e) => setSubtype(e.target.value)} />
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.products.materialNumber')}</span>
                <input
                  className={inputClass}
                  value={materialNumber}
                  maxLength={64}
                  onChange={(e) => setMaterialNumber(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.products.slicerPreset')}</span>
                <input
                  className={inputClass}
                  value={slicerFilamentName}
                  maxLength={100}
                  onChange={(e) => setSlicerFilamentName(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.products.slicerPresetId')}</span>
                <input
                  className={inputClass}
                  value={slicerFilament}
                  maxLength={50}
                  onChange={(e) => setSlicerFilament(e.target.value)}
                />
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.products.nozzleTemp')}</span>
                <div className="flex items-center gap-1">
                  <input
                    className={inputClass}
                    inputMode="numeric"
                    value={tempMin}
                    placeholder="min"
                    onChange={(e) => setTempMin(e.target.value)}
                  />
                  <span className="text-bambu-gray">–</span>
                  <input
                    className={inputClass}
                    inputMode="numeric"
                    value={tempMax}
                    placeholder="max"
                    onChange={(e) => setTempMax(e.target.value)}
                  />
                </div>
              </label>
              <label className="block">
                <span className="text-xs text-bambu-gray">{t('inventory.products.note')}</span>
                <input className={inputClass} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} />
              </label>
            </div>
            {product && product.spool_count > 0 && (
              <p className="text-xs text-bambu-gray">{t('inventory.products.writeThroughHint')}</p>
            )}
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Sizes */}
            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium text-white">{t('inventory.products.sizes')}</h3>
                <button
                  onClick={() => addSize()}
                  className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded flex items-center gap-1"
                >
                  <Plus className="w-3.5 h-3.5" />
                  {t('inventory.products.addSize')}
                </button>
              </div>
              <div className="flex flex-wrap gap-1">
                {QUICK_SIZES.filter((g) => !sizes.some((s) => toInt(s.label_weight) === g)).map((grams) => (
                  <button
                    key={grams}
                    onClick={() => addSize(grams)}
                    className="px-2 py-0.5 text-xs rounded-full border border-dashed border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-green"
                  >
                    + {formatWeight(grams)}
                  </button>
                ))}
              </div>
              {sizes.length === 0 ? (
                <p className="text-xs text-bambu-gray py-2">{t('inventory.products.noSizes')}</p>
              ) : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-xs text-bambu-gray">
                      <th className="text-left font-medium pb-1">{t('inventory.products.labelWeight')}</th>
                      <th className="text-left font-medium pb-1">{t('inventory.products.coreWeight')}</th>
                      <th className="text-left font-medium pb-1">{t('inventory.products.pricePerSpool')}</th>
                      <th className="text-left font-medium pb-1">{t('inventory.products.vat')}</th>
                      <th className="text-right font-medium pb-1">{t('inventory.products.perKg')}</th>
                      <th className="w-8" />
                    </tr>
                  </thead>
                  <tbody>
                    {sizes.map((size) => {
                      const perKg = costPerKg(parsePrice(size.price), toInt(size.label_weight) ?? 0);
                      const blocked = spoolsInColumn(size.key) > 0;
                      return (
                        <tr key={size.key}>
                          <td className="pr-2 py-1">
                            <div className="flex items-center gap-1">
                              <input
                                className={smallInputClass}
                                inputMode="numeric"
                                value={size.label_weight}
                                onChange={(e) => updateSize(size.key, { label_weight: e.target.value })}
                                aria-label={t('inventory.products.labelWeight')}
                              />
                              <span className="text-xs text-bambu-gray">g</span>
                            </div>
                          </td>
                          <td className="pr-2 py-1">
                            <div className="flex items-center gap-1">
                              <input
                                className={smallInputClass}
                                inputMode="numeric"
                                value={size.core_weight}
                                onChange={(e) => updateSize(size.key, { core_weight: e.target.value })}
                                aria-label={t('inventory.products.coreWeight')}
                              />
                              <span className="text-xs text-bambu-gray">g</span>
                            </div>
                          </td>
                          <td className="pr-2 py-1">
                            <div className="flex items-center gap-1">
                              <span className="text-xs text-bambu-gray">{currency}</span>
                              <input
                                className={smallInputClass}
                                inputMode="decimal"
                                value={size.price}
                                placeholder="0.00"
                                onChange={(e) => updateSize(size.key, { price: e.target.value })}
                                aria-label={t('inventory.products.pricePerSpool')}
                              />
                            </div>
                          </td>
                          <td className="pr-2 py-1">
                            <select
                              className={smallInputClass}
                              value={size.price_vat_included ? 'gross' : 'net'}
                              onChange={(e) => updateSize(size.key, { price_vat_included: e.target.value === 'gross' })}
                              aria-label={t('inventory.products.vat')}
                            >
                              <option value="gross">{t('inventory.products.gross')}</option>
                              <option value="net">{t('inventory.products.net')}</option>
                            </select>
                          </td>
                          <td className="py-1 text-right text-xs text-bambu-gray whitespace-nowrap">
                            {perKg !== null ? `${formatMoney(perKg, currency)}/kg` : '–'}
                          </td>
                          <td className="py-1 text-right">
                            <button
                              onClick={() => removeSize(size.key)}
                              disabled={blocked}
                              title={blocked ? t('inventory.products.cannotRemove', { count: spoolsInColumn(size.key) }) : undefined}
                              className="p-1 rounded text-red-500 hover:bg-red-500/10 disabled:opacity-30 disabled:cursor-not-allowed"
                              aria-label={t('common.delete')}
                            >
                              <Trash2 className="w-4 h-4" />
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </section>

            {/* Colours */}
            <section className="space-y-2">
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium text-white">{t('inventory.products.colors')}</h3>
                <button
                  onClick={addColor}
                  className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded flex items-center gap-1"
                >
                  <Plus className="w-3.5 h-3.5" />
                  {t('inventory.products.addColor')}
                </button>
              </div>
              {colors.length === 0 ? (
                <p className="text-xs text-bambu-gray py-2">{t('inventory.products.noColors')}</p>
              ) : (
                <div className="space-y-1">
                  {colors.map((color) => {
                    const blocked = spoolsInRow(color.key) > 0;
                    return (
                      <div key={color.key} className="flex items-center gap-2">
                        <input
                          type="color"
                          value={`#${color.hex ?? '808080'}`}
                          onChange={(e) => updateColor(color.key, { hex: e.target.value.slice(1).toUpperCase() })}
                          className="w-8 h-8 rounded border border-bambu-dark-tertiary bg-transparent cursor-pointer shrink-0"
                          aria-label={t('inventory.products.colorValue')}
                        />
                        <input
                          className={smallInputClass}
                          value={color.color_name}
                          maxLength={100}
                          placeholder={t('inventory.products.colorName')}
                          onChange={(e) => updateColor(color.key, { color_name: e.target.value })}
                        />
                        <button
                          onClick={() => removeColor(color.key)}
                          disabled={blocked}
                          title={blocked ? t('inventory.products.cannotRemove', { count: spoolsInRow(color.key) }) : undefined}
                          className="p-1 rounded text-red-500 hover:bg-red-500/10 disabled:opacity-30 disabled:cursor-not-allowed shrink-0"
                          aria-label={t('common.delete')}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          </div>

          {/* Matrix */}
          {sizes.length > 0 && colors.length > 0 && (
            <section className="space-y-2">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <div>
                  <h3 className="text-sm font-medium text-white">{t('inventory.products.variants')}</h3>
                  <p className="text-xs text-bambu-gray">
                    {t('inventory.products.variantsHint', { count: tickedCount })}
                  </p>
                </div>
                <div className="flex gap-1">
                  <button
                    onClick={() => setAllCells(true)}
                    className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded"
                  >
                    {t('inventory.products.allOn')}
                  </button>
                  <button
                    onClick={() => setAllCells(false)}
                    className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded"
                  >
                    {t('inventory.products.allOff')}
                  </button>
                </div>
              </div>
              <div className="overflow-x-auto border border-bambu-dark-tertiary rounded-lg">
                <table className="text-sm min-w-full">
                  <thead className="bg-bambu-dark">
                    <tr>
                      <th className="px-3 py-2 text-left text-xs text-bambu-gray font-medium">{t('inventory.products.color')}</th>
                      {sizes.map((size) => (
                        <th key={size.key} className="px-3 py-2 text-center text-xs text-bambu-gray font-medium whitespace-nowrap">
                          {toInt(size.label_weight) ? formatWeight(toInt(size.label_weight) ?? 0) : '?'}
                          <div className="font-normal text-bambu-gray/70">
                            {parsePrice(size.price) !== null ? formatMoney(parsePrice(size.price), currency) : '–'}
                          </div>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {colors.map((color) => (
                      <tr key={color.key} className="border-t border-bambu-dark-tertiary">
                        <td className="px-3 py-1.5">
                          <div className="flex items-center gap-2 whitespace-nowrap">
                            <FilamentSwatch
                              rgba={color.hex ? `${color.hex}${color.alpha}` : null}
                              effectSize="table"
                              className="w-4 h-4"
                            />
                            <span className="text-white">{color.color_name || (color.hex ? `#${color.hex}` : '?')}</span>
                          </div>
                        </td>
                        {sizes.map((size) => {
                          const key = cellKey(color.key, size.key);
                          const ticked = cells.has(key);
                          const spools = spoolsInCell(key);
                          const existing = existingByCell.get(key);
                          return (
                            <td key={size.key} className="px-2 py-1.5 text-center align-middle">
                              <div className="flex flex-col items-center gap-1">
                                <input
                                  type="checkbox"
                                  checked={ticked}
                                  disabled={ticked && spools > 0}
                                  onChange={() => toggleCell(key)}
                                  title={spools > 0 ? t('inventory.products.cannotRemove', { count: spools }) : undefined}
                                  className="w-4 h-4 accent-bambu-green disabled:opacity-60"
                                  aria-label={`${color.color_name} ${size.label_weight} g`}
                                />
                                {ticked && (
                                  <input
                                    className="w-20 px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-xs text-center placeholder-bambu-gray/50 focus:border-bambu-green focus:outline-none"
                                    inputMode="decimal"
                                    value={cells.get(key) ?? ''}
                                    placeholder={parsePrice(size.price) !== null ? priceText(parsePrice(size.price)) : currency}
                                    title={t('inventory.products.priceOverrideHint')}
                                    onChange={(e) =>
                                      setCells((prev) => new Map(prev).set(key, e.target.value))
                                    }
                                  />
                                )}
                                {existing && existing.spool_count > 0 && (
                                  <span className="text-[10px] text-bambu-green whitespace-nowrap">
                                    {t('inventory.products.cellStock', {
                                      count: existing.spool_count,
                                      stock: formatStock(existing.remaining_g),
                                    })}
                                  </span>
                                )}
                              </div>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* Suppliers: on the product, not on every spool */}
          <section className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-medium text-white">{t('inventory.products.suppliers')}</h3>
                <p className="text-xs text-bambu-gray">{t('inventory.products.suppliersHint')}</p>
              </div>
              <button
                onClick={() =>
                  setSupplierRows((prev) => [
                    ...prev,
                    { key: newKey('p'), supplier_id: null, article_number: '', preferred: prev.length === 0, prices: {} },
                  ])
                }
                disabled={allSuppliers.length === 0 || supplierRows.length >= allSuppliers.length}
                className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded flex items-center gap-1 disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
              >
                <Plus className="w-3.5 h-3.5" />
                {t('inventory.products.addSupplier')}
              </button>
            </div>
            {allSuppliers.length === 0 ? (
              <p className="text-xs text-bambu-gray py-1">{t('inventory.products.noSuppliersYet')}</p>
            ) : supplierRows.length === 0 ? (
              <p className="text-xs text-bambu-gray py-1">{t('inventory.products.noProductSuppliers')}</p>
            ) : (
              <div className="overflow-x-auto border border-bambu-dark-tertiary rounded-lg">
                <table className="text-sm min-w-full">
                  <thead className="bg-bambu-dark">
                    <tr className="text-xs text-bambu-gray">
                      <th className="px-2 py-2 text-left font-medium" title={t('inventory.products.preferredHint')}>
                        <Star className="w-3.5 h-3.5" />
                      </th>
                      <th className="px-2 py-2 text-left font-medium">{t('inventory.products.supplier')}</th>
                      <th className="px-2 py-2 text-left font-medium">{t('inventory.products.articleNumber')}</th>
                      {sizes.map((size) => (
                        <th key={size.key} className="px-2 py-2 text-left font-medium whitespace-nowrap">
                          {toInt(size.label_weight) ? formatWeight(toInt(size.label_weight) ?? 0) : '?'}
                        </th>
                      ))}
                      <th className="w-8" />
                    </tr>
                  </thead>
                  <tbody>
                    {supplierRows.map((row) => (
                      <tr key={row.key} className="border-t border-bambu-dark-tertiary">
                        <td className="px-2 py-1.5">
                          <input
                            type="radio"
                            name="preferred-supplier"
                            checked={row.preferred}
                            onChange={() =>
                              setSupplierRows((prev) => prev.map((r) => ({ ...r, preferred: r.key === row.key })))
                            }
                            className="accent-bambu-green"
                            aria-label={t('inventory.products.preferredHint')}
                          />
                        </td>
                        <td className="px-2 py-1.5 min-w-[10rem]">
                          <select
                            className={smallInputClass}
                            value={row.supplier_id ?? ''}
                            onChange={(e) =>
                              setSupplierRows((prev) =>
                                prev.map((r) =>
                                  r.key === row.key ? { ...r, supplier_id: e.target.value ? Number(e.target.value) : null } : r,
                                ),
                              )
                            }
                          >
                            <option value="">{t('inventory.products.choose')}</option>
                            {allSuppliers
                              .filter(
                                (s) => s.id === row.supplier_id || !supplierRows.some((r) => r.supplier_id === s.id),
                              )
                              .map((s) => (
                                <option key={s.id} value={s.id}>
                                  {s.name}
                                </option>
                              ))}
                          </select>
                        </td>
                        <td className="px-2 py-1.5 min-w-[8rem]">
                          <input
                            className={smallInputClass}
                            value={row.article_number}
                            maxLength={100}
                            placeholder={t('inventory.products.articleNumber')}
                            onChange={(e) =>
                              setSupplierRows((prev) =>
                                prev.map((r) => (r.key === row.key ? { ...r, article_number: e.target.value } : r)),
                              )
                            }
                          />
                        </td>
                        {sizes.map((size) => (
                          <td key={size.key} className="px-2 py-1.5 min-w-[6rem]">
                            <div className="flex items-center gap-1">
                              <span className="text-xs text-bambu-gray">{currency}</span>
                              <input
                                className={smallInputClass}
                                inputMode="decimal"
                                value={row.prices[size.key] ?? ''}
                                placeholder={size.price || '–'}
                                onChange={(e) =>
                                  setSupplierRows((prev) =>
                                    prev.map((r) =>
                                      r.key === row.key ? { ...r, prices: { ...r.prices, [size.key]: e.target.value } } : r,
                                    ),
                                  )
                                }
                                aria-label={`${t('inventory.products.pricePerSpool')} ${size.label_weight} g`}
                              />
                            </div>
                          </td>
                        ))}
                        <td className="px-2 py-1.5 text-right">
                          <button
                            onClick={() =>
                              setSupplierRows((prev) => {
                                const rest = prev.filter((r) => r.key !== row.key);
                                // Keep one usual supplier when the usual one goes.
                                if (row.preferred && rest.length > 0 && !rest.some((r) => r.preferred)) {
                                  rest[0] = { ...rest[0], preferred: true };
                                }
                                return rest;
                              })
                            }
                            className="p-1 rounded text-red-500 hover:bg-red-500/10"
                            aria-label={t('common.delete')}
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {allSuppliers.length === 0 && (
              <p className="text-xs text-bambu-gray flex items-center gap-1">
                <Store className="w-3.5 h-3.5" />
                {t('inventory.products.suppliersWhere')}
              </p>
            )}
          </section>

          {/* Codes learnt at intake */}
          {codes.length > 0 && (
            <section className="space-y-2">
              <h3 className="text-sm font-medium text-white">{t('inventory.products.codes')}</h3>
              <div className="flex flex-wrap gap-2">
                {codes.map((code) => (
                  <span
                    key={code.id}
                    className="inline-flex items-center gap-1.5 px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded-full text-bambu-gray"
                  >
                    <Barcode className="w-3.5 h-3.5" />
                    <span className="font-mono text-white">{code.code}</span>
                    <span>→ {variantName(code.variantId)}</span>
                    <button
                      onClick={() => removeCode(code.id)}
                      className="text-bambu-gray hover:text-red-500"
                      aria-label={t('common.delete')}
                    >
                      <X className="w-3 h-3" />
                    </button>
                  </span>
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 px-6 py-4 border-t border-bambu-dark-tertiary">
          <div>
            {product && (
              <button
                onClick={() => setConfirmDelete(true)}
                className="px-3 py-2 text-sm rounded-lg text-red-500 hover:bg-red-500/10 flex items-center gap-1.5"
              >
                <Trash2 className="w-4 h-4" />
                {t('inventory.products.deleteProduct')}
              </button>
            )}
          </div>
          <div className="flex gap-2">
            <button
              onClick={onClose}
              className="px-3 py-2 rounded-lg text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary"
            >
              {t('common.cancel')}
            </button>
            <button
              onClick={handleSave}
              disabled={saving}
              className="px-4 py-2 bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 flex items-center gap-1.5 disabled:opacity-50"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              {t('common.save')}
            </button>
          </div>
        </div>
      </div>

      {confirmDelete && product && (
        <ConfirmModal
          title={t('inventory.products.deleteProduct')}
          message={t('inventory.products.deleteConfirm', { name: product.label, count: product.spool_count })}
          confirmText={t('common.delete')}
          variant="danger"
          onConfirm={handleDelete}
          onCancel={() => setConfirmDelete(false)}
        />
      )}
    </div>
  );
}
