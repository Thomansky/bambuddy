import { describe, expect, it } from 'vitest';
import type { FilamentProduct } from '../../../api/client';
import { costPerKg, intakePrice, parsePrice, preferredSupplierId } from '../../../components/products/productUtils';

// Product master data (#3165): the price goods-in proposes.
const product: FilamentProduct = {
  id: 1,
  label: 'Bambu Lab PLA Matte',
  brand: 'Bambu Lab',
  material: 'PLA',
  subtype: 'Matte',
  material_number: '52',
  slicer_filament: null,
  slicer_filament_name: null,
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  sizes: [{ id: 10, label_weight: 1000, core_weight: 250, core_weight_catalog_id: null, price: 20, price_vat_included: true }],
  colors: [],
  variants: [],
  suppliers: [
    { supplier_id: 7, supplier_name: 'Shop A', article_number: null, preferred: false, prices: [{ size_id: 10, price: 17.5 }] },
    { supplier_id: 8, supplier_name: 'Shop B', article_number: 'X', preferred: true, prices: [] },
  ],
  spool_count: 0,
  remaining_g: 0,
};
const size = product.sizes[0];
const plain = { id: 1, color_id: 1, size_id: 10, price_override: null, effective_price: 20, cost_per_kg: 20, codes: [], spool_count: 0, remaining_g: 0 };

describe('intakePrice', () => {
  it('takes the supplier price for the size when there is one', () => {
    expect(intakePrice(product, plain, size, 7)).toBe(17.5);
  });

  it('falls back to the list price when the supplier has none for the size', () => {
    expect(intakePrice(product, plain, size, 8)).toBe(20);
    expect(intakePrice(product, plain, size, null)).toBe(20);
  });

  it("lets a combination's own price win", () => {
    expect(intakePrice(product, { ...plain, price_override: 26 }, size, 7)).toBe(26);
  });
});

describe('small helpers', () => {
  it('finds the usual supplier', () => {
    expect(preferredSupplierId(product)).toBe(8);
  });

  it('reads a decimal comma', () => {
    expect(parsePrice('11,50')).toBe(11.5);
    expect(parsePrice('')).toBeNull();
    expect(parsePrice('abc')).toBeNull();
  });

  it('turns a spool price into cost per kg', () => {
    expect(costPerKg(90, 5000)).toBe(18);
    expect(costPerKg(null, 1000)).toBeNull();
  });
});
