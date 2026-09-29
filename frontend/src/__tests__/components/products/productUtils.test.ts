import { describe, expect, it } from 'vitest';
import type { FilamentProduct } from '../../../api/client';
import {
  costPerKg,
  parsePrice,
  presetModels,
  productMatches,
  usualSupplier,
} from '../../../components/products/productUtils';

// Product master data (#3165): the small helpers the product views share.
const product: FilamentProduct = {
  id: 1,
  label: 'Bambu Lab PLA Matte',
  brand: 'Bambu Lab',
  material: 'PLA',
  subtype: 'Matte',
  material_number: '52',
  slicer_filament: null,
  slicer_filament_name: null,
  presets: [],
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: null,
  sizes: [{ id: 10, label_weight: 1000, core_weight: 250, core_weight_catalog_id: null, price: 20, price_vat_included: true }],
  colors: [],
  variants: [],
  suppliers: [
    { supplier_id: 7, supplier_name: 'Shop A', preferred: false },
    { supplier_id: 8, supplier_name: 'Shop B', preferred: true },
  ],
  spool_count: 0,
  remaining_g: 0,
};

describe('suppliers', () => {
  it('names the starred one as the usual supplier, else the first', () => {
    expect(usualSupplier(product)?.supplier_id).toBe(8);
    const unstarred = { ...product, suppliers: product.suppliers.map((s) => ({ ...s, preferred: false })) };
    expect(usualSupplier(unstarred)?.supplier_id).toBe(7);
    expect(usualSupplier({ ...product, suppliers: [] })).toBeUndefined();
  });

  it('are found by the search', () => {
    expect(productMatches(product, 'shop b')).toBe(true);
    expect(productMatches(product, 'shop c')).toBe(false);
  });
});

describe('presetModels', () => {
  const h2d = { printer_model: 'H2D', slicer_filament: 'GFSA01_H2D', slicer_filament_name: 'Bambu PLA Matte @BBL H2D' };

  it('names the model of the own preset, then each preset per model', () => {
    const withPresets = {
      ...product,
      slicer_filament: 'GFSA01_H2S',
      slicer_filament_name: 'Bambu PLA Matte @BBL H2S',
      presets: [h2d],
    };
    expect(presetModels(withPresets)).toEqual([
      { model: 'H2S', name: 'Bambu PLA Matte @BBL H2S' },
      { model: 'H2D', name: 'Bambu PLA Matte @BBL H2D' },
    ]);
  });

  it('lets a preset for the model win over the own one naming it', () => {
    const both = {
      ...product,
      slicer_filament: 'GFSA01_H2D',
      slicer_filament_name: 'Bambu PLA Matte @BBL H2D',
      presets: [{ ...h2d, slicer_filament_name: 'My PLA @BBL H2D' }],
    };
    expect(presetModels(both)).toEqual([{ model: 'H2D', name: 'My PLA @BBL H2D' }]);
  });

  it('reads the model past the "(Custom)" a stored name ends in', () => {
    const custom = {
      ...product,
      slicer_filament: 'PFUS0123',
      slicer_filament_name: 'XIONEER HIPS VLX 90 @BBL H2D 0.4 nozzle (Custom)',
    };
    expect(presetModels(custom).map((entry) => entry.model)).toEqual(['H2D']);
  });

  it('gives an own preset that names no model no badge', () => {
    expect(presetModels({ ...product, slicer_filament: 'GFL01', slicer_filament_name: 'Bambu PLA Matte' })).toEqual([]);
  });
});

describe('small helpers', () => {
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
