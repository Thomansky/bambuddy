import type {
  FilamentProduct,
  FilamentProductColor,
  FilamentProductSize,
  FilamentVariant,
  ProductReorderLine,
} from '../../api/client';

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

/** What one spool of a size costs at a supplier, if the product says. */
export function supplierPrice(product: FilamentProduct, supplierId: number | null, sizeId: number): number | null {
  if (supplierId === null) return null;
  const row = product.suppliers.find((s) => s.supplier_id === supplierId);
  return row?.prices.find((p) => p.size_id === sizeId)?.price ?? null;
}

/** The price goods-in proposes: a combination's own price wins, then the
 *  supplier's price for the size, then the size's list price. Mirrors
 *  intake_price on the backend. */
export function intakePrice(
  product: FilamentProduct,
  variant: FilamentVariant,
  size: FilamentProductSize,
  supplierId: number | null,
): number | null {
  if (variant.price_override !== null) return variant.price_override;
  return supplierPrice(product, supplierId, size.id) ?? size.price;
}

export function preferredSupplierId(product: FilamentProduct): number | null {
  return product.suppliers.find((s) => s.preferred)?.supplier_id ?? null;
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

/** What one spool of a reorder line costs at the chosen supplier — the list
 *  price without one, or where the supplier has none of its own. */
export function reorderPrice(line: ProductReorderLine, supplierId: number | null): number | null {
  if (supplierId === null) return line.list_price;
  return line.suppliers.find((s) => s.supplier_id === supplierId)?.price ?? line.list_price;
}
