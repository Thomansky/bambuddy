import type {
  FilamentProduct,
  FilamentProductColor,
  FilamentProductSize,
  FilamentProductSupplier,
  FilamentVariant,
} from '../../api/client';
import type { ColumnConfig } from '../ColumnConfigModal';
import { extractPresetModel, matchesPrinterModelSuffix } from '../../utils/slicerPrinterMatch';

/** Product master data (#3165) — small shared helpers for the views. */

/** Products by material number (numeric), unnumbered ones last by name. */
export function compareProducts(a: FilamentProduct, b: FilamentProduct): number {
  if (a.material_number && b.material_number) {
    const byNumber = a.material_number.localeCompare(b.material_number, undefined, { numeric: true });
    if (byNumber !== 0) return byNumber;
  } else if (a.material_number || b.material_number) {
    return a.material_number ? -1 : 1;
  }
  return a.label.localeCompare(b.label);
}

export function formatWeight(grams: number): string {
  if (grams >= 1000 && grams % 100 === 0) {
    return `${(grams / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} kg`;
  }
  return `${grams} g`;
}

export function formatStock(grams: number): string {
  if (grams >= 1000) return `${(grams / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} kg`;
  return `${Math.round(grams)} g`;
}

export function formatMoney(value: number | null | undefined, symbol: string): string {
  if (value === null || value === undefined) return '–';
  return `${symbol}${value.toFixed(2)}`;
}

export function colorLabel(color: FilamentProductColor): string {
  return color.color_name || (color.rgba ? `#${color.rgba.slice(0, 6)}` : '?');
}

export interface VariantParts {
  variant: FilamentVariant;
  color: FilamentProductColor;
  size: FilamentProductSize;
}

export function variantParts(product: FilamentProduct, variantId: number): VariantParts | null {
  const variant = product.variants.find((v) => v.id === variantId);
  if (!variant) return null;
  const color = product.colors.find((c) => c.id === variant.color_id);
  const size = product.sizes.find((s) => s.id === variant.size_id);
  if (!color || !size) return null;
  return { variant, color, size };
}

export function findVariant(product: FilamentProduct, colorId: number, sizeId: number): FilamentVariant | undefined {
  return product.variants.find((v) => v.color_id === colorId && v.size_id === sizeId);
}

/** Price per spool → cost per kg, the unit spools and print costing use. */
export function costPerKg(price: number | null, labelWeight: number): number | null {
  if (price === null || !labelWeight) return null;
  return Math.round((price / (labelWeight / 1000)) * 100) / 100;
}

/** Parse a user-typed price; accepts a decimal comma. Empty → null. */
export function parsePrice(text: string): number | null {
  const cleaned = text.trim().replace(',', '.');
  if (!cleaned) return null;
  const value = Number(cleaned);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

export function priceText(value: number | null | undefined): string {
  return value === null || value === undefined ? '' : String(value);
}

/** "1 kg", or "1 kg Refill" for filament without a spool of its own. */
export function formatSizeLabel(labelWeight: number, refill: boolean, refillWord: string): string {
  return refill ? `${formatWeight(labelWeight)} ${refillWord}` : formatWeight(labelWeight);
}

/** A product's "prices as of" date (YYYY-MM-DD) the way the user reads dates. */
export function formatPriceDate(iso: string | null | undefined): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? '');
  if (!match) return '';
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])).toLocaleDateString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  });
}

/** "Bambu PLA Matte @BBL H2S" → "Bambu PLA Matte". */
export function presetStem(name: string): string {
  return name.split('@')[0].trim();
}

/** The printer model a preset name is for ("… @BBL H2D 0.4 nozzle" → "H2D"),
 *  read past the "(Custom)" a stored display name ends in. */
export function presetModelOf(name: string, printerModels: Record<string, string> = {}): string | null {
  return extractPresetModel(name.replace(/\s*\([^()]*\)\s*$/, ''), printerModels);
}

/** The printer models a product has a slicer preset for: the one its own
 *  preset names ("@BBL H2S"), then its presets per model, which win where
 *  both name the same model. */
export function presetModels(
  product: FilamentProduct,
  printerModels: Record<string, string> = {},
): { model: string; name: string }[] {
  const rows = (product.presets ?? []).map((row) => ({
    model: row.printer_model,
    name: row.slicer_filament_name || row.slicer_filament,
  }));
  const own = product.slicer_filament_name || product.slicer_filament;
  const ownModel = own ? presetModelOf(own, printerModels) : null;
  if (!own || !ownModel || rows.some((row) => matchesPrinterModelSuffix(ownModel, row.model))) return rows;
  return [{ model: ownModel, name: own }, ...rows];
}

/** How well a support material worked with a product: 1 (poor) to 4 (very good). */
export const MAX_RATING = 4;
const RATING_WORDS = ['ratingPoor', 'ratingFair', 'ratingGood', 'ratingVeryGood'];
type TFn = (key: string, opts?: Record<string, unknown>) => string;

/** The word for one rating ("Good"). */
export function ratingWord(t: TFn, value: number): string {
  return t(`inventory.products.${RATING_WORDS[value - 1]}`);
}

/** "Good (3 of 4)", or "Not rated yet". */
export function ratingText(t: TFn, value: number | null): string {
  return value
    ? t('inventory.products.ratingOf', { rating: ratingWord(t, value), stars: value, max: MAX_RATING })
    : t('inventory.products.notRated');
}

/** Whether a product is made to be printed as support — "Support for PLA",
 *  PLA-S, PVA, BVOH, HIPS — so the support picker offers it first. */
export function looksLikeSupport(product: Pick<FilamentProduct, 'material' | 'subtype'>): boolean {
  return /support|\b(?:pva|bvoh|hips)\b|-s\b/i.test(`${product.material} ${product.subtype ?? ''}`);
}

/** The supplier a product is usually bought from: the starred one, else the first. */
export function usualSupplier(product: FilamentProduct): FilamentProductSupplier | undefined {
  return product.suppliers.find((s) => s.preferred) ?? product.suppliers[0];
}

export interface ProductTotals {
  /** Spools the targets ask for, over the combinations that have one. */
  target: number;
  onOrder: number;
  /** Spools short of the targets once what is on order is counted. */
  shortfall: number;
  /** Combinations below their target. */
  below: number;
}

export function productTotals(product: FilamentProduct): ProductTotals {
  return product.variants.reduce<ProductTotals>(
    (sum, v) => ({
      target: sum.target + (v.min_stock ?? 0),
      onOrder: sum.onOrder + v.on_order,
      shortfall: sum.shortfall + v.shortfall,
      below: sum.below + (v.shortfall > 0 ? 1 : 0),
    }),
    { target: 0, onOrder: 0, shortfall: 0, below: 0 },
  );
}

/** Cheapest and dearest spool of the product, or null when nothing is priced. */
export function priceRange(product: FilamentProduct): [number, number] | null {
  const prices = product.variants.map((v) => v.effective_price).filter((p): p is number => p !== null);
  if (prices.length === 0) return null;
  return [Math.min(...prices), Math.max(...prices)];
}

/** Whether a product answers a search: its names, number, colours, suppliers,
 *  their article numbers and the codes learnt at intake. */
export function productMatches(product: FilamentProduct, needle: string): boolean {
  const query = needle.trim().toLowerCase();
  if (!query) return true;
  const haystack = [
    product.label,
    product.brand,
    product.material,
    product.subtype,
    product.material_number,
    product.slicer_filament_name,
    product.note,
    ...product.colors.map((c) => c.color_name),
    ...product.suppliers.map((s) => s.supplier_name),
    ...product.variants.flatMap((v) => v.codes.map((c) => c.code)),
  ];
  return haystack.some((value) => (value ?? '').toLowerCase().includes(query));
}

/** Sort values compare numbers as numbers and text numerically ("2" before "15"). */
export function compareSortValues(a: string | number, b: string | number): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

/** A stored column layout brought up to date: columns that no longer exist
 *  go, columns added since land at their default place, and the user's order
 *  and visibility are kept for the rest. */
export function mergeColumnConfig(stored: ColumnConfig[] | null, defaults: ColumnConfig[]): ColumnConfig[] {
  if (!stored) return defaults.map((c) => ({ ...c }));
  const known = new Set(defaults.map((c) => c.id));
  const merged = stored.filter((c) => known.has(c.id)).map((c) => ({ ...c }));
  const present = new Set(merged.map((c) => c.id));
  defaults.forEach((col, index) => {
    if (present.has(col.id)) return;
    let insertAt = merged.length;
    for (let i = index - 1; i >= 0; i--) {
      const at = merged.findIndex((c) => c.id === defaults[i].id);
      if (at !== -1) {
        insertAt = at + 1;
        break;
      }
    }
    merged.splice(insertAt, 0, { ...col });
  });
  return merged;
}

/** The reorder list's lines. Under the shopping list's key, since they are
 *  its rows: whatever refreshes the shopping list refreshes them too. */
export const ORDER_LINES_KEY = ['shopping-list', 'orders'];

/** Invalidated whenever the reorder list changes: the list itself (and the
 *  forecast's shopping list) and what shows how much is on order. */
export const ORDER_QUERY_KEYS = [['shopping-list'], ['filament-products'], ['filament-products-reorder'], ['reorder-line']];
