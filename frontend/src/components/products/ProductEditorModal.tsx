import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Barcode, Boxes, Check, Loader2, Plus, Star, Store, Trash2, X } from 'lucide-react';
import { api, ApiError } from '../../api/client';
import type { FilamentProduct, FilamentProductInput, FilamentVariant } from '../../api/client';
import type { FilamentOption } from '../spool-form/types';
import { useToast } from '../../contexts/ToastContext';
import { ConfirmModal } from '../ConfirmModal';
import { FilamentSwatch } from '../FilamentSwatch';
import { SuppliersModal } from '../SuppliersModal';
import { PresetPicker } from '../spool-form/PresetPicker';
import { findPresetOption } from '../spool-form/utils';
import { usePresetOptions } from './usePresetOptions';
import { ModelBadge } from './ModelBadge';
import { getCurrencySymbol } from '../../utils/currency';
import { localDateKey } from '../../utils/date';
import { extractPresetModel, matchesPrinterModelSuffix } from '../../utils/slicerPrinterMatch';
import {
  costPerKg,
  formatMoney,
  formatStock,
  formatWeight,
  parsePrice,
  presetModelOf,
  presetStem,
  priceText,
} from './productUtils';

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

/** The preset for one printer model; the product's own covers the rest. */
interface ModelPresetRow {
  printer_model: string;
  /** Empty while the row waits for a preset; such a row is not saved. */
  slicer_filament: string;
  slicer_filament_name: string;
}

/** A supplier the product has been bought from. */
interface SupplierRow {
  supplier_id: number;
  /** The usual supplier; the reorder list starts from it. */
  preferred: boolean;
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

/** Sizes lightest first; one without a weight yet waits at the end. The sort
 *  is stable, so two rows with the same weight keep their order. */
function sortSizes(rows: SizeRow[]): SizeRow[] {
  const weight = (row: SizeRow) => toInt(row.label_weight) ?? Number.MAX_SAFE_INTEGER;
  return [...rows].sort((a, b) => weight(a) - weight(b));
}

// Product master data (#3165): one product, its colours and sizes, and the
// matrix of combinations that exist. Saving sends the whole document; the
// backend writes master-data changes through to the spools (never the price).
export function ProductEditorModal({ product, onClose, onSaved }: ProductEditorModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const { data: allSuppliers = [], isLoading: loadingSuppliers } = useQuery({
    queryKey: ['suppliers'],
    queryFn: api.getSuppliers,
  });
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
  // When the prices were last checked: the standard prices at the sizes and
  // the special prices in the matrix alike. YYYY-MM-DD, or '' for none.
  const [priceDate, setPriceDate] = useState(product?.price_date ?? '');
  // A size's price is the manufacturer's price for one spool. What a delivery
  // actually cost is confirmed at goods-in, so the suppliers carry no prices.
  const [sizes, setSizes] = useState<SizeRow[]>(() =>
    sortSizes(
      product?.sizes.map((s) => ({
        id: s.id,
        key: `s${s.id}`,
        label_weight: String(s.label_weight),
        core_weight: String(s.core_weight),
        price: priceText(s.price),
        price_vat_included: s.price_vat_included,
      })) ?? [],
    ),
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
      product?.suppliers.map((row, index) => ({
        supplier_id: row.supplier_id,
        // One of them is always the usual one.
        preferred: row.preferred || (index === 0 && !product.suppliers.some((r) => r.preferred)),
      })) ?? [],
  );
  // Target stock per ticked cell (#3165): how many spools should be on the shelf.
  const [minStock, setMinStock] = useState<Map<string, string>>(
    () =>
      new Map(
        (product?.variants ?? [])
          .filter((v) => v.min_stock !== null && v.min_stock !== undefined)
          .map((v) => [cellKey(`c${v.color_id}`, `s${v.size_id}`), String(v.min_stock)]),
      ),
  );
  const [matrixMode, setMatrixMode] = useState<'price' | 'target'>('price');
  const [targetForAll, setTargetForAll] = useState('');
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // The supplier master list, opened on top so a supplier can be created
  // while setting a product up.
  const [suppliersOpen, setSuppliersOpen] = useState(false);
  const { options: presetOptions, loading: loadingPresets } = usePresetOptions();
  const selectedPreset = useMemo(() => findPresetOption(slicerFilament, presetOptions), [slicerFilament, presetOptions]);
  // A preset per printer model (#3165): a cloud or Orca preset is bound to a
  // model, so the product's own is only right on the model it names.
  const [modelPresets, setModelPresets] = useState<ModelPresetRow[]>(() =>
    (product?.presets ?? []).map((row) => ({
      printer_model: row.printer_model,
      slicer_filament: row.slicer_filament,
      slicer_filament_name: row.slicer_filament_name ?? '',
    })),
  );
  const { data: printers = [] } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  // Reads the model out of a preset name; the same query the spool dialog uses.
  const { data: printerModels } = useQuery({
    queryKey: ['slicerPrinterModels'],
    queryFn: api.getSlicerPrinterModels,
    staleTime: Infinity,
  });
  // The model the product's own preset is for ("@BBL H2S"), if its name says.
  const ownModel = useMemo(() => {
    const name = selectedPreset?.name || slicerFilamentName;
    return name ? presetModelOf(name, printerModels ?? {}) : null;
  }, [selectedPreset, slicerFilamentName, printerModels]);
  // The fleet's models that have no preset yet.
  const addableModels = useMemo(
    () =>
      [...new Set(printers.map((p) => (p.model ?? '').trim()).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b))
        .filter(
          (model) =>
            !modelPresets.some((row) => row.printer_model === model) &&
            !(ownModel && matchesPrinterModelSuffix(ownModel, model)),
        ),
    [printers, modelPresets, ownModel],
  );

  // Existing variants by cell, for stock counts and the removal guard.
  const existingByCell = useMemo(() => {
    const map = new Map<string, FilamentVariant>();
    for (const variant of product?.variants ?? []) map.set(cellKey(`c${variant.color_id}`, `s${variant.size_id}`), variant);
    return map;
  }, [product]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !confirmDelete && !suppliersOpen) onClose();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onClose, confirmDelete, suppliersOpen]);

  const closeSuppliers = () => {
    setSuppliersOpen(false);
    // The master list edits outside react-query, so the picker below refetches.
    queryClient.invalidateQueries({ queryKey: ['suppliers'] });
  };

  const spoolsInCell = (key: string) => existingByCell.get(key)?.spool_count ?? 0;
  const spoolsInRow = (colorKey: string) =>
    sizes.reduce((sum, size) => sum + spoolsInCell(cellKey(colorKey, size.key)), 0);
  const spoolsInColumn = (sizeKey: string) =>
    colors.reduce((sum, color) => sum + spoolsInCell(cellKey(color.key, sizeKey)), 0);

  const addSize = (grams?: number) => {
    if (grams && sizes.some((s) => toInt(s.label_weight) === grams)) return;
    const key = newKey('s');
    setSizes((prev) =>
      sortSizes([
        ...prev,
        {
          id: null,
          key,
          label_weight: grams ? String(grams) : '',
          core_weight: grams && grams <= 1000 ? '250' : '',
          price: '',
          price_vat_included: prev[0]?.price_vat_included ?? true,
        },
      ]),
    );
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

  const applyTargetToAll = () => {
    const value = targetForAll.trim();
    setMinStock((prev) => {
      const next = new Map(prev);
      for (const key of cells.keys()) {
        if (value) next.set(key, value);
        else next.delete(key);
      }
      return next;
    });
  };

  // The product's own rows name their supplier before the master list is in.
  const supplierName = (id: number) =>
    allSuppliers.find((s) => s.id === id)?.name ??
    product?.suppliers.find((s) => s.supplier_id === id)?.supplier_name ??
    '?';
  const addableSuppliers = allSuppliers.filter((s) => !supplierRows.some((r) => r.supplier_id === s.id));

  const addSupplier = (id: number) =>
    setSupplierRows((prev) =>
      prev.some((r) => r.supplier_id === id) ? prev : [...prev, { supplier_id: id, preferred: prev.length === 0 }],
    );

  const makeUsual = (id: number) =>
    setSupplierRows((prev) => prev.map((r) => ({ ...r, preferred: r.supplier_id === id })));

  const removeSupplier = (id: number) =>
    setSupplierRows((prev) => {
      const rest = prev.filter((r) => r.supplier_id !== id);
      // The usual supplier going hands the star to the first one left.
      if (rest.length > 0 && !rest.some((r) => r.preferred)) rest[0] = { ...rest[0], preferred: true };
      return rest;
    });

  // A model is offered only the presets made for it, as in the spool dialog;
  // one that names no model stays, and so does the one already chosen.
  const optionsForModel = (model: string, selected: string): FilamentOption[] => {
    const list = presetOptions.filter((option) => {
      const presetModel = extractPresetModel(option.name, printerModels ?? {});
      return !presetModel || matchesPrinterModelSuffix(presetModel, model);
    });
    const kept = selected ? findPresetOption(selected, presetOptions) : undefined;
    return kept && !list.includes(kept) ? [kept, ...list] : list;
  };

  // A model added starts on the variant of the product's own preset made for
  // it ("Bambu PLA Matte @BBL H2D" beside "… @BBL H2S"), when there is one.
  const addModelPreset = (model: string) => {
    const stem = presetStem(selectedPreset?.name || slicerFilamentName).toLowerCase();
    const candidates = stem
      ? presetOptions.filter((option) => {
          const presetModel = extractPresetModel(option.name, printerModels ?? {});
          return (
            presetModel !== null &&
            matchesPrinterModelSuffix(presetModel, model) &&
            presetStem(option.name).toLowerCase() === stem
          );
        })
      : [];
    // Bambu names a preset for another nozzle size ("… 0.2 nozzle"); the
    // plain one is the standard nozzle's.
    const match = candidates.find((option) => !/\b[\d.]+\s*nozzle\b/i.test(option.name)) ?? candidates[0];
    setModelPresets((prev) => [
      ...prev,
      { printer_model: model, slicer_filament: match?.code ?? '', slicer_filament_name: match?.name ?? '' },
    ]);
  };

  const setModelPreset = (model: string, option: FilamentOption | null) =>
    setModelPresets((prev) =>
      prev.map((row) =>
        row.printer_model === model
          ? { ...row, slicer_filament: option?.code ?? '', slicer_filament_name: option?.name ?? '' }
          : row,
      ),
    );

  const removeModelPreset = (model: string) =>
    setModelPresets((prev) => prev.filter((row) => row.printer_model !== model));

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
      presets: modelPresets
        .filter((row) => row.slicer_filament)
        .map((row) => ({
          printer_model: row.printer_model,
          slicer_filament: row.slicer_filament,
          slicer_filament_name: row.slicer_filament_name || null,
        })),
      nozzle_temp_min: toInt(tempMin),
      nozzle_temp_max: toInt(tempMax),
      note: note.trim() || null,
      price_date: priceDate || null,
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
          return {
            color_key: colorKey,
            size_key: sizeKey,
            price_override: parsePrice(price),
            min_stock: toInt(minStock.get(key) ?? ''),
          };
        })
        .filter((v) => colorKeys.has(v.color_key) && sizeKeys.has(v.size_key)),
      suppliers: supplierRows.map((row) => ({ supplier_id: row.supplier_id, preferred: row.preferred })),
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
        className="relative w-full max-w-[1800px] mx-4 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl max-h-[95vh] flex flex-col"
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
              <div className="block sm:col-span-2 space-y-1.5">
                <span className="text-xs text-bambu-gray flex items-center gap-1">
                  {t('inventory.products.slicerPresets')}
                  {loadingPresets && <Loader2 className="w-3 h-3 animate-spin" />}
                </span>
                {/* The product's own preset: every model without one below uses it. */}
                <div className="flex items-center gap-2">
                  <div className="flex-1 min-w-0">
                    <PresetPicker
                      value={selectedPreset?.code ?? ''}
                      options={presetOptions}
                      inheritLabel={
                        // A preset the lists do not know (cloud not connected) is
                        // still shown by its stored name rather than as none.
                        !selectedPreset && slicerFilament
                          ? `${slicerFilamentName || slicerFilament} (${slicerFilament})`
                          : t('inventory.products.noPreset')
                      }
                      onChange={(option) => {
                        setSlicerFilament(option?.code ?? '');
                        setSlicerFilamentName(option?.displayName ?? '');
                      }}
                      ariaLabel={t('inventory.products.slicerPreset')}
                    />
                  </div>
                  <ModelBadge
                    model={ownModel ?? t('inventory.products.allModels')}
                    muted={!ownModel}
                    title={t('inventory.products.presetDefaultHint')}
                  />
                  {modelPresets.length > 0 && <span className="w-7 shrink-0" />}
                </div>
                {modelPresets.map((row) => {
                  const known = findPresetOption(row.slicer_filament, presetOptions);
                  return (
                    <div key={row.printer_model} className="flex items-center gap-2">
                      <div className="flex-1 min-w-0">
                        <PresetPicker
                          value={known?.code ?? ''}
                          options={optionsForModel(row.printer_model, row.slicer_filament)}
                          inheritLabel={
                            !known && row.slicer_filament
                              ? `${row.slicer_filament_name || row.slicer_filament} (${row.slicer_filament})`
                              : t('inventory.products.choosePreset')
                          }
                          onChange={(option) => setModelPreset(row.printer_model, option)}
                          ariaLabel={t('inventory.products.presetFor', { model: row.printer_model })}
                        />
                      </div>
                      <ModelBadge model={row.printer_model} muted={!row.slicer_filament} />
                      <button
                        onClick={() => removeModelPreset(row.printer_model)}
                        className="p-1 rounded text-red-500 hover:bg-red-500/10 shrink-0"
                        aria-label={t('inventory.products.removePresetFor', { model: row.printer_model })}
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  );
                })}
                {(addableModels.length > 0 || selectedPreset) && (
                  <div className="flex flex-wrap items-center gap-2">
                    {addableModels.length > 0 && (
                      <select
                        value=""
                        onChange={(e) => {
                          if (e.target.value) addModelPreset(e.target.value);
                        }}
                        aria-label={t('inventory.products.addPresetForModel')}
                        className="px-2 py-1 text-xs bg-bambu-dark border border-dashed border-bambu-dark-tertiary rounded-full text-bambu-gray hover:text-white focus:border-bambu-green focus:outline-none"
                      >
                        <option value="">+ {t('inventory.products.addPresetForModel')}</option>
                        {addableModels.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    )}
                    {selectedPreset && (
                      <span className="text-[11px] text-bambu-gray">
                        {t('inventory.products.slicerPresetId')}: <span className="font-mono">{selectedPreset.code}</span>
                      </span>
                    )}
                  </div>
                )}
              </div>
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

          <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-6">
            {/* Sizes */}
            <section className="space-y-2">
              <div className="flex items-center justify-between gap-2 flex-wrap">
                <h3 className="text-sm font-medium text-white">{t('inventory.products.sizes')}</h3>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <label
                    className="flex items-center gap-1.5 text-xs text-bambu-gray"
                    title={t('inventory.products.priceDateHint')}
                  >
                    {t('inventory.products.priceDate')}
                    <input
                      type="date"
                      value={priceDate}
                      onChange={(e) => setPriceDate(e.target.value)}
                      className="px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-xs focus:border-bambu-green focus:outline-none [color-scheme:dark]"
                      aria-label={t('inventory.products.priceDate')}
                    />
                  </label>
                  <button
                    onClick={() => setPriceDate(localDateKey(new Date()))}
                    className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded"
                  >
                    {t('inventory.products.priceDateToday')}
                  </button>
                <button
                  onClick={() => addSize()}
                  className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded flex items-center gap-1"
                >
                  <Plus className="w-3.5 h-3.5" />
                  {t('inventory.products.addSize')}
                </button>
                </div>
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
                <div className="overflow-x-auto">
                <table className="w-full min-w-[36rem] text-sm">
                  <thead>
                    <tr className="text-xs text-bambu-gray">
                      <th className="text-left font-medium pb-1">{t('inventory.products.labelWeight')}</th>
                      <th className="text-left font-medium pb-1">{t('inventory.products.coreWeight')}</th>
                      <th className="text-left font-medium pb-1" title={t('inventory.products.listPriceHint')}>
                        {t('inventory.products.listPrice')}
                      </th>
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
                                onBlur={() => setSizes((prev) => sortSizes(prev))}
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
                                aria-label={t('inventory.products.listPrice')}
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
                </div>
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
                    {matrixMode === 'target'
                      ? t('inventory.products.targetsHint')
                      : t('inventory.products.variantsHint', { count: tickedCount })}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-1">
                  <div className="flex bg-bambu-dark border border-bambu-dark-tertiary rounded-md p-0.5" role="group">
                    {(['price', 'target'] as const).map((mode) => (
                      <button
                        key={mode}
                        onClick={() => setMatrixMode(mode)}
                        aria-pressed={matrixMode === mode}
                        className={`px-2 py-0.5 text-xs rounded transition-colors ${
                          matrixMode === mode ? 'bg-bambu-dark-tertiary text-white' : 'text-bambu-gray hover:text-white'
                        }`}
                      >
                        {mode === 'price' ? t('inventory.products.matrixPrices') : t('inventory.products.matrixTargets')}
                      </button>
                    ))}
                  </div>
                  {matrixMode === 'target' && (
                    <div className="flex items-center gap-1">
                      <input
                        className="w-12 px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-xs text-center focus:border-bambu-green focus:outline-none"
                        inputMode="numeric"
                        value={targetForAll}
                        placeholder="–"
                        onChange={(e) => setTargetForAll(e.target.value.replace(/[^0-9]/g, ''))}
                        aria-label={t('inventory.products.targetForAll')}
                      />
                      <button
                        onClick={applyTargetToAll}
                        className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded"
                      >
                        {t('inventory.products.targetForAll')}
                      </button>
                    </div>
                  )}
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
                                {ticked && matrixMode === 'target' && (
                                  <input
                                    className="w-14 px-1.5 py-0.5 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-xs text-center placeholder-bambu-gray/50 focus:border-bambu-green focus:outline-none"
                                    inputMode="numeric"
                                    value={minStock.get(key) ?? ''}
                                    placeholder="–"
                                    title={t('inventory.products.minStock')}
                                    aria-label={`${t('inventory.products.minStock')} ${color.color_name} ${size.label_weight} g`}
                                    onChange={(e) =>
                                      setMinStock((prev) => new Map(prev).set(key, e.target.value.replace(/[^0-9]/g, '')))
                                    }
                                  />
                                )}
                                {ticked && matrixMode === 'price' && (
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

          {/* Suppliers: where the product has been bought — on the product,
              not on every spool, and without prices or article numbers */}
          <section className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <div>
                <h3 className="text-sm font-medium text-white">{t('inventory.products.suppliers')}</h3>
                <p className="text-xs text-bambu-gray">{t('inventory.products.suppliersHint')}</p>
              </div>
              <button
                onClick={() => setSuppliersOpen(true)}
                className="px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white rounded flex items-center gap-1 shrink-0"
              >
                <Store className="w-3.5 h-3.5" />
                {t('inventory.products.manageSuppliers')}
              </button>
            </div>
            {supplierRows.length === 0 && allSuppliers.length === 0 && !loadingSuppliers ? (
              <div className="flex flex-wrap items-center gap-2 py-1">
                <p className="text-xs text-bambu-gray">{t('inventory.products.noSuppliersYet')}</p>
                <button
                  onClick={() => setSuppliersOpen(true)}
                  className="px-2 py-1 text-xs bg-bambu-green text-white rounded flex items-center gap-1 hover:bg-bambu-green/80"
                >
                  <Plus className="w-3.5 h-3.5" />
                  {t('inventory.products.createSuppliers')}
                </button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                {supplierRows.length === 0 && (
                  <span className="text-xs text-bambu-gray">{t('inventory.products.noProductSuppliers')}</span>
                )}
                {supplierRows.map((row) => {
                  const name = supplierName(row.supplier_id);
                  return (
                    <span
                      key={row.supplier_id}
                      className={`inline-flex items-center gap-1 pl-1 pr-1.5 py-0.5 text-sm rounded-full border ${
                        row.preferred ? 'border-bambu-green/60 bg-bambu-green/10' : 'border-bambu-dark-tertiary bg-bambu-dark'
                      }`}
                    >
                      <button
                        onClick={() => makeUsual(row.supplier_id)}
                        aria-pressed={row.preferred}
                        title={t('inventory.products.preferredHint')}
                        aria-label={t('inventory.products.makeUsual', { name })}
                        className={`p-0.5 rounded-full ${row.preferred ? 'text-bambu-green' : 'text-bambu-gray hover:text-white'}`}
                      >
                        <Star className={`w-3.5 h-3.5 ${row.preferred ? 'fill-current' : ''}`} />
                      </button>
                      <span className="text-white">{name}</span>
                      <button
                        onClick={() => removeSupplier(row.supplier_id)}
                        aria-label={t('inventory.products.removeSupplier', { name })}
                        className="p-0.5 rounded-full text-bambu-gray hover:text-red-500"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </span>
                  );
                })}
                {addableSuppliers.length > 0 && (
                  <select
                    value=""
                    onChange={(e) => {
                      if (e.target.value) addSupplier(Number(e.target.value));
                    }}
                    aria-label={t('inventory.products.addSupplier')}
                    className="px-2 py-1 text-xs bg-bambu-dark border border-dashed border-bambu-dark-tertiary rounded-full text-bambu-gray hover:text-white focus:border-bambu-green focus:outline-none"
                  >
                    <option value="">+ {t('inventory.products.addSupplier')}</option>
                    {addableSuppliers.map((s) => (
                      <option key={s.id} value={s.id}>
                        {s.name}
                      </option>
                    ))}
                  </select>
                )}
              </div>
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

      <SuppliersModal open={suppliersOpen} onClose={closeSuppliers} />

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
