import { Fragment, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Barcode, Boxes, ChevronDown, ChevronRight, Loader2, PackagePlus, Pencil, Plus, Search, WandSparkles, X } from 'lucide-react';
import { api } from '../../api/client';
import type { FilamentProduct } from '../../api/client';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import { ConversionModal } from './ConversionModal';
import { ProductEditorModal } from './ProductEditorModal';
import { colorLabel, compareProducts, findVariant, formatMoney, formatStock, formatWeight } from './productUtils';

interface ProductsPanelProps {
  /** Open goods-in, optionally on one product. */
  onIntake: (productId: number | null) => void;
  /** Active spools that belong to no product yet. */
  unassignedCount?: number;
}

// The "Products" view of the inventory (#3165): every product with its
// colours and sizes, and — unfolded — the stock of each variant, the
// virtual twin of the shelf.
export function ProductsPanel({ onIntake, unassignedCount = 0 }: ProductsPanelProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { data: products = [], isLoading } = useQuery({
    queryKey: ['filament-products'],
    queryFn: api.getFilamentProducts,
  });
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const currency = getCurrencySymbol(settings?.currency || 'USD');
  const [search, setSearch] = useState('');
  // undefined = closed, null = a new product
  const [editing, setEditing] = useState<FilamentProduct | null | undefined>(undefined);
  const [converting, setConverting] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const matching = needle
      ? products.filter(
          (p) =>
            p.label.toLowerCase().includes(needle) ||
            (p.material_number ?? '').toLowerCase().includes(needle) ||
            p.colors.some((c) => (c.color_name ?? '').toLowerCase().includes(needle)),
        )
      : products;
    return [...matching].sort(compareProducts);
  }, [products, search]);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['filament-products'] });
    queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
  };

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const totals = useMemo(
    () => ({
      variants: products.reduce((sum, p) => sum + p.variants.length, 0),
      spools: products.reduce((sum, p) => sum + p.spool_count, 0),
    }),
    [products],
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center justify-between">
        <div className="relative flex-1 max-w-md w-full">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('inventory.products.search')}
            className="w-full pl-10 pr-8 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
          />
          {search && (
            <button
              onClick={() => setSearch('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
              aria-label={t('common.clear')}
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setConverting(true)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary hover:text-white transition-colors"
          >
            <WandSparkles className="w-4 h-4" />
            {t('inventory.products.takeOver')}
          </button>
          <button
            onClick={() => onIntake(null)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary hover:text-white transition-colors"
          >
            <PackagePlus className="w-4 h-4" />
            {t('inventory.products.intakeTitle')}
          </button>
          <button
            onClick={() => setEditing(null)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80 transition-colors"
          >
            <Plus className="w-4 h-4" />
            {t('inventory.products.newProduct')}
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16">
          <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
        </div>
      ) : products.length === 0 ? (
        <div className="bg-bambu-dark-secondary rounded-lg p-8 text-center space-y-3">
          <Boxes className="w-10 h-10 text-bambu-gray mx-auto" />
          <h3 className="text-white font-medium">{t('inventory.products.emptyTitle')}</h3>
          <p className="text-sm text-bambu-gray max-w-xl mx-auto">{t('inventory.products.emptyText')}</p>
          <div className="flex justify-center gap-2 pt-1">
            <button
              onClick={() => setConverting(true)}
              className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80"
            >
              <WandSparkles className="w-4 h-4" />
              {t('inventory.products.takeOver')}
            </button>
            <button
              onClick={() => setEditing(null)}
              className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary hover:text-white"
            >
              <Plus className="w-4 h-4" />
              {t('inventory.products.newProduct')}
            </button>
          </div>
        </div>
      ) : (
        <>
          {unassignedCount > 0 && (
            <div className="flex flex-wrap items-center gap-3 px-4 py-2.5 rounded-lg bg-amber-500/10 border border-amber-500/30">
              <span className="text-sm text-amber-200 flex-1 min-w-[12rem]">
                {t('inventory.products.unassignedBanner', { count: unassignedCount })}
              </span>
              <button
                onClick={() => setConverting(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-amber-100 border border-amber-500/40 rounded-lg hover:bg-amber-500/20"
              >
                <WandSparkles className="w-4 h-4" />
                {t('inventory.products.takeOver')}
              </button>
            </div>
          )}
          <p className="text-xs text-bambu-gray">
            {t('inventory.products.summary', {
              products: products.length,
              variants: totals.variants,
              spools: totals.spools,
            })}
          </p>
          <div className="bg-bambu-dark-secondary rounded-lg overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="bg-bambu-dark">
                <tr className="text-xs text-bambu-gray">
                  <th className="w-8" />
                  <th className="px-3 py-2 text-left font-medium">{t('inventory.products.materialNumberShortHeader')}</th>
                  <th className="px-3 py-2 text-left font-medium">{t('inventory.products.product')}</th>
                  <th className="px-3 py-2 text-left font-medium hidden md:table-cell">{t('inventory.products.colors')}</th>
                  <th className="px-3 py-2 text-left font-medium hidden lg:table-cell">{t('inventory.products.sizes')}</th>
                  <th className="px-3 py-2 text-right font-medium">{t('inventory.products.spools')}</th>
                  <th className="px-3 py-2 text-right font-medium hidden sm:table-cell">{t('inventory.products.stock')}</th>
                  <th className="px-3 py-2 w-24" />
                </tr>
              </thead>
              <tbody>
                {visible.map((product) => {
                  const open = expanded.has(product.id);
                  return (
                    <Fragment key={product.id}>
                      <tr
                        className="border-t border-bambu-dark-tertiary hover:bg-bambu-dark/50 cursor-pointer"
                        onClick={() => toggle(product.id)}
                      >
                        <td className="pl-3 py-2 text-bambu-gray">
                          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </td>
                        <td className="px-3 py-2 font-mono text-white">{product.material_number ?? '—'}</td>
                        <td className="px-3 py-2 text-white">{product.label}</td>
                        <td className="px-3 py-2 hidden md:table-cell">
                          <div className="flex items-center gap-1">
                            {product.colors.slice(0, 10).map((color) => (
                              <FilamentSwatch
                                key={color.id}
                                rgba={color.rgba}
                                extraColors={color.extra_colors}
                                effectType={color.effect_type}
                                effectSize="table"
                                className="w-4 h-4"
                                title={colorLabel(color)}
                              />
                            ))}
                            {product.colors.length > 10 && (
                              <span className="text-xs text-bambu-gray">+{product.colors.length - 10}</span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-2 hidden lg:table-cell">
                          <div className="flex flex-wrap gap-1">
                            {product.sizes.map((size) => (
                              <span
                                key={size.id}
                                className="px-1.5 py-0.5 text-xs rounded bg-bambu-dark text-bambu-gray whitespace-nowrap"
                              >
                                {formatWeight(size.label_weight)}
                                {size.price !== null && ` · ${formatMoney(size.price, currency)}`}
                              </span>
                            ))}
                          </div>
                        </td>
                        <td className="px-3 py-2 text-right text-white">{product.spool_count}</td>
                        <td className="px-3 py-2 text-right text-bambu-gray hidden sm:table-cell">
                          {formatStock(product.remaining_g)}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                            <button
                              onClick={() => onIntake(product.id)}
                              className="p-1.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white"
                              title={t('inventory.products.intakeTitle')}
                              aria-label={t('inventory.products.intakeTitle')}
                            >
                              <PackagePlus className="w-4 h-4" />
                            </button>
                            <button
                              onClick={() => setEditing(product)}
                              className="p-1.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-white"
                              title={t('common.edit')}
                              aria-label={t('common.edit')}
                            >
                              <Pencil className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                      {open && (
                        <tr className="bg-bambu-dark/40">
                          <td colSpan={8} className="px-6 py-3">
                            <StockMatrix product={product} currency={currency} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
                {visible.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-4 py-8 text-center text-bambu-gray">
                      {t('inventory.products.noMatch')}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      )}

      {editing !== undefined && (
        <ProductEditorModal
          product={editing}
          onClose={() => setEditing(undefined)}
          onSaved={() => {
            setEditing(undefined);
            refresh();
          }}
        />
      )}
      {converting && (
        <ConversionModal
          onClose={() => setConverting(false)}
          onDone={() => {
            setConverting(false);
            refresh();
          }}
        />
      )}
    </div>
  );
}

/** Colours × sizes with the stock of each variant. */
function StockMatrix({ product, currency }: { product: FilamentProduct; currency: string }) {
  const { t } = useTranslation();
  if (product.colors.length === 0 || product.sizes.length === 0) {
    return <p className="text-xs text-bambu-gray">{t('inventory.products.noVariants')}</p>;
  }
  return (
    <div className="space-y-2">
      <table className="text-sm">
        <thead>
          <tr className="text-xs text-bambu-gray">
            <th className="pr-6 py-1 text-left font-medium">{t('inventory.products.color')}</th>
            {product.sizes.map((size) => (
              <th key={size.id} className="px-4 py-1 text-center font-medium whitespace-nowrap">
                {formatWeight(size.label_weight)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {product.colors.map((color) => (
            <tr key={color.id}>
              <td className="pr-6 py-1">
                <div className="flex items-center gap-2 whitespace-nowrap">
                  <FilamentSwatch
                    rgba={color.rgba}
                    extraColors={color.extra_colors}
                    effectType={color.effect_type}
                    effectSize="table"
                    className="w-4 h-4"
                  />
                  <span className="text-white">{colorLabel(color)}</span>
                </div>
              </td>
              {product.sizes.map((size) => {
                const variant = findVariant(product, color.id, size.id);
                if (!variant) {
                  return (
                    <td key={size.id} className="px-4 py-1 text-center text-bambu-gray/30">
                      ·
                    </td>
                  );
                }
                return (
                  <td
                    key={size.id}
                    className="px-4 py-1 text-center whitespace-nowrap"
                    title={`${formatMoney(variant.effective_price, currency)} · ${formatMoney(variant.cost_per_kg, currency)}/kg`}
                  >
                    {variant.spool_count > 0 ? (
                      <span className="text-white">
                        {variant.spool_count}
                        <span className="text-xs text-bambu-gray ml-1">({formatStock(variant.remaining_g)})</span>
                      </span>
                    ) : (
                      <span className="text-bambu-gray">0</span>
                    )}
                    {variant.codes.length > 0 && (
                      <Barcode className="inline w-3 h-3 ml-1 text-bambu-gray" aria-label={t('inventory.products.codes')} />
                    )}
                    {variant.price_override !== null && (
                      <span className="block text-[10px] text-amber-300">{formatMoney(variant.price_override, currency)}</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {product.suppliers.length > 0 && (
        <p className="text-xs text-bambu-gray">
          {t('inventory.products.suppliers')}:{' '}
          {product.suppliers
            .map((s) => `${s.supplier_name}${s.article_number ? ` (${s.article_number})` : ''}${s.preferred ? ' ★' : ''}`)
            .join(' · ')}
        </p>
      )}
      {(product.nozzle_temp_min || product.slicer_filament_name || product.note) && (
        <p className="text-xs text-bambu-gray">
          {[
            product.nozzle_temp_min && product.nozzle_temp_max
              ? `${product.nozzle_temp_min}–${product.nozzle_temp_max} °C`
              : null,
            product.slicer_filament_name,
            product.note,
          ]
            .filter(Boolean)
            .join(' · ')}
        </p>
      )}
    </div>
  );
}
