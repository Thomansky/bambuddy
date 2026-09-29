import { Fragment, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Barcode,
  Boxes,
  ChevronDown,
  ChevronRight,
  Columns,
  Loader2,
  PackagePlus,
  Pencil,
  Plus,
  Search,
  WandSparkles,
  X,
} from 'lucide-react';
import { api } from '../../api/client';
import type { FilamentProduct, SpoolCatalogEntry } from '../../api/client';
import { ColumnConfigModal, type ColumnConfig } from '../ColumnConfigModal';
import { FilamentSwatch } from '../FilamentSwatch';
import { getCurrencySymbol } from '../../utils/currency';
import {
  colorLabel,
  compareProducts,
  compareSortValues,
  findVariant,
  formatMoney,
  formatStock,
  formatWeight,
  mergeColumnConfig,
  priceRange,
  productMatches,
  productTotals,
  usualSupplier,
} from './productUtils';

interface ProductsPanelProps {
  /** Open goods-in, optionally on one product. */
  onIntake: (productId: number | null) => void;
  /** Open the editor on a product, or on a new one (null). */
  onEdit: (product: FilamentProduct | null) => void;
  /** Open the take-over of the existing spools. */
  onConvert: () => void;
  /** Active spools that belong to no product yet. */
  unassignedCount?: number;
}

type StockFilter = 'all' | 'in_stock' | 'empty' | 'below';
type SortState = { column: string; direction: 'asc' | 'desc' } | null;
type TFn = (key: string, opts?: Record<string, unknown>) => string;

interface CellCtx {
  product: FilamentProduct;
  t: TFn;
  currency: string;
  catalogMap: Record<number, SpoolCatalogEntry>;
}

const COLUMNS_KEY = 'bambuddy-products-columns';
const SORT_KEY = 'bambuddy-products-sort';
// Filter value for "has none of these".
const NONE = '__none__';

// The product list's columns (#3165), built like the spool table's: the same
// dialog picks and orders them, and every one of them sorts.
const DEFAULT_COLUMNS: ColumnConfig[] = [
  { id: 'material_number', label: 'No.', visible: true },
  { id: 'brand', label: 'Manufacturer', visible: true },
  { id: 'material', label: 'Material', visible: true },
  { id: 'subtype', label: 'Type', visible: true },
  { id: 'name', label: 'Name', visible: false },
  { id: 'colors', label: 'Colours', visible: true },
  { id: 'sizes', label: 'Sizes', visible: true },
  { id: 'supplier', label: 'Supplier', visible: true },
  { id: 'price', label: 'Price/spool', visible: false },
  { id: 'spool_type', label: 'Spool type', visible: false },
  { id: 'preset', label: 'Slicer preset', visible: false },
  { id: 'nozzle_temp', label: 'Nozzle temp.', visible: false },
  { id: 'variants', label: 'Variants', visible: false },
  { id: 'spools', label: 'Spools', visible: true },
  { id: 'stock', label: 'Stock', visible: true },
  { id: 'target', label: 'Target', visible: false },
  { id: 'on_order', label: 'Ordered', visible: false },
  { id: 'shortfall', label: 'Missing', visible: true },
  { id: 'note', label: 'Note', visible: false },
];

const columnHeaders: Record<string, (t: TFn) => string> = {
  material_number: (t) => t('inventory.products.columns.number'),
  brand: (t) => t('inventory.products.columns.brand'),
  material: (t) => t('inventory.products.columns.material'),
  subtype: (t) => t('inventory.products.columns.subtype'),
  name: (t) => t('inventory.products.columns.name'),
  colors: (t) => t('inventory.products.columns.colors'),
  sizes: (t) => t('inventory.products.columns.sizes'),
  supplier: (t) => t('inventory.products.columns.supplier'),
  price: (t) => t('inventory.products.columns.price'),
  spool_type: (t) => t('inventory.products.columns.spoolType'),
  preset: (t) => t('inventory.products.columns.preset'),
  nozzle_temp: (t) => t('inventory.products.columns.nozzleTemp'),
  variants: (t) => t('inventory.products.columns.variants'),
  spools: (t) => t('inventory.products.columns.spools'),
  stock: (t) => t('inventory.products.columns.stock'),
  target: (t) => t('inventory.products.columns.target'),
  on_order: (t) => t('inventory.products.columns.onOrder'),
  shortfall: (t) => t('inventory.products.columns.shortfall'),
  note: (t) => t('inventory.products.columns.note'),
};

const EMPTY = <span className="text-sm text-bambu-gray/50">–</span>;

function spoolTypeNames(product: FilamentProduct, catalogMap: Record<number, SpoolCatalogEntry>): string {
  const names = new Set<string>();
  for (const size of product.sizes) {
    const entry = size.core_weight_catalog_id != null ? catalogMap[size.core_weight_catalog_id] : undefined;
    if (entry) names.add(entry.name);
  }
  return [...names].join(', ');
}

const columnCells: Record<string, (ctx: CellCtx) => ReactNode> = {
  material_number: ({ product }) =>
    product.material_number ? <span className="text-sm font-mono text-white">{product.material_number}</span> : EMPTY,
  brand: ({ product }) => (product.brand ? <span className="text-sm text-white">{product.brand}</span> : EMPTY),
  material: ({ product }) => <span className="text-sm text-white">{product.material}</span>,
  subtype: ({ product }) => (product.subtype ? <span className="text-sm text-bambu-gray">{product.subtype}</span> : EMPTY),
  name: ({ product }) => <span className="text-sm text-white">{product.label}</span>,
  colors: ({ product }) => (
    <div className="flex items-center gap-1" title={product.colors.map(colorLabel).join(', ')}>
      {product.colors.slice(0, 8).map((color) => (
        <FilamentSwatch
          key={color.id}
          rgba={color.rgba}
          extraColors={color.extra_colors}
          effectType={color.effect_type}
          effectSize="table"
          className="w-4 h-4"
        />
      ))}
      {product.colors.length > 8 && <span className="text-xs text-bambu-gray">+{product.colors.length - 8}</span>}
    </div>
  ),
  sizes: ({ product, currency }) => (
    <div className="flex flex-wrap gap-1">
      {product.sizes.map((size) => (
        <span key={size.id} className="px-1.5 py-0.5 text-xs rounded bg-bambu-dark text-bambu-gray whitespace-nowrap">
          {formatWeight(size.label_weight)}
          {size.price !== null && ` · ${formatMoney(size.price, currency)}`}
        </span>
      ))}
    </div>
  ),
  supplier: ({ product }) => {
    const usual = usualSupplier(product);
    if (!usual) return EMPTY;
    const others = product.suppliers.length - 1;
    return (
      <span
        className="text-sm text-white whitespace-nowrap"
        title={product.suppliers.map((s) => s.supplier_name).join(', ')}
      >
        {usual.supplier_name}
        {others > 0 && <span className="text-xs text-bambu-gray ml-1">+{others}</span>}
      </span>
    );
  },
  price: ({ product, currency }) => {
    const range = priceRange(product);
    if (!range) return EMPTY;
    const [low, high] = range;
    return (
      <span className="text-sm text-bambu-gray whitespace-nowrap">
        {low === high ? formatMoney(low, currency) : `${formatMoney(low, currency)} – ${formatMoney(high, currency)}`}
      </span>
    );
  },
  spool_type: ({ product, catalogMap }) => {
    const names = spoolTypeNames(product, catalogMap);
    return names ? <span className="text-sm text-bambu-gray">{names}</span> : EMPTY;
  },
  preset: ({ product }) => {
    const preset = product.slicer_filament_name || product.slicer_filament;
    return preset ? <span className="text-sm text-bambu-gray">{preset}</span> : EMPTY;
  },
  nozzle_temp: ({ product }) =>
    product.nozzle_temp_min && product.nozzle_temp_max ? (
      <span className="text-sm text-bambu-gray whitespace-nowrap">
        {product.nozzle_temp_min}–{product.nozzle_temp_max} °C
      </span>
    ) : (
      EMPTY
    ),
  variants: ({ product }) => <span className="text-sm text-bambu-gray">{product.variants.length}</span>,
  spools: ({ product }) => <span className="text-sm text-white">{product.spool_count}</span>,
  stock: ({ product }) => <span className="text-sm text-bambu-gray whitespace-nowrap">{formatStock(product.remaining_g)}</span>,
  target: ({ product }) => {
    const { target } = productTotals(product);
    return target > 0 ? <span className="text-sm text-bambu-gray">{target}</span> : EMPTY;
  },
  on_order: ({ product }) => {
    const { onOrder } = productTotals(product);
    return onOrder > 0 ? <span className="text-sm text-amber-300">{onOrder}</span> : EMPTY;
  },
  shortfall: ({ product, t }) => {
    const { shortfall, below } = productTotals(product);
    if (shortfall === 0) return EMPTY;
    return (
      <span
        className="text-sm font-medium text-red-400 whitespace-nowrap"
        title={t('inventory.products.reorder.belowTarget', { count: below })}
      >
        −{shortfall}
      </span>
    );
  },
  note: ({ product }) =>
    product.note ? (
      <span className="text-sm text-bambu-gray max-w-[180px] truncate block" title={product.note}>
        {product.note}
      </span>
    ) : (
      EMPTY
    ),
};

const columnSortValues: Record<
  string,
  (product: FilamentProduct, catalogMap: Record<number, SpoolCatalogEntry>) => string | number
> = {
  // Products without a number sort after every numbered one.
  material_number: (p) => p.material_number || '￿',
  brand: (p) => p.brand || '',
  material: (p) => p.material,
  subtype: (p) => p.subtype || '',
  name: (p) => p.label,
  colors: (p) => p.colors.length,
  sizes: (p) => (p.sizes.length ? Math.min(...p.sizes.map((s) => s.label_weight)) : 0),
  supplier: (p) => usualSupplier(p)?.supplier_name || '￿',
  price: (p) => priceRange(p)?.[0] ?? Number.MAX_VALUE,
  spool_type: (p, catalogMap) => spoolTypeNames(p, catalogMap) || '￿',
  preset: (p) => p.slicer_filament_name || p.slicer_filament || '￿',
  nozzle_temp: (p) => p.nozzle_temp_min ?? Number.MAX_VALUE,
  variants: (p) => p.variants.length,
  spools: (p) => p.spool_count,
  stock: (p) => p.remaining_g,
  target: (p) => productTotals(p).target,
  on_order: (p) => productTotals(p).onOrder,
  shortfall: (p) => productTotals(p).shortfall,
  note: (p) => p.note || '￿',
};

function loadColumns(): ColumnConfig[] {
  try {
    const stored = localStorage.getItem(COLUMNS_KEY);
    return mergeColumnConfig(stored ? (JSON.parse(stored) as ColumnConfig[]) : null, DEFAULT_COLUMNS);
  } catch {
    return mergeColumnConfig(null, DEFAULT_COLUMNS);
  }
}

function saveColumns(config: ColumnConfig[]) {
  try {
    localStorage.setItem(COLUMNS_KEY, JSON.stringify(config));
  } catch {
    // Ignore: the layout just is not remembered.
  }
}

function loadSort(): SortState {
  try {
    const stored = localStorage.getItem(SORT_KEY);
    return stored ? (JSON.parse(stored) as SortState) : null;
  } catch {
    return null;
  }
}

function saveSort(state: SortState) {
  try {
    if (state) localStorage.setItem(SORT_KEY, JSON.stringify(state));
    else localStorage.removeItem(SORT_KEY);
  } catch {
    // Ignore: the order just is not remembered.
  }
}

const chipClass = (active: boolean) =>
  `px-3 py-1.5 rounded-lg border text-xs font-medium transition-colors cursor-pointer focus:outline-none ${
    active
      ? 'bg-bambu-green/20 text-bambu-green border-bambu-green/30'
      : 'bg-transparent text-bambu-gray border-bambu-dark-tertiary hover:bg-bambu-dark-tertiary'
  }`;

const segmentClass = (active: boolean, tone: 'green' | 'red' = 'green') =>
  `px-3 py-1.5 text-xs font-medium transition-colors ${
    active
      ? tone === 'red'
        ? 'bg-red-500/20 text-red-400'
        : 'bg-bambu-green/20 text-bambu-green'
      : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
  }`;

// The "Products" section of the filament stock (#3165): every product in a
// table built like the spool list — search, filter chips, configurable and
// sortable columns — and, unfolded, the stock of each colour × size.
export function ProductsPanel({ onIntake, onEdit, onConvert, unassignedCount = 0 }: ProductsPanelProps) {
  const { t } = useTranslation();
  const { data: products = [], isLoading } = useQuery({
    queryKey: ['filament-products'],
    queryFn: api.getFilamentProducts,
  });
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const { data: catalogEntries = [] } = useQuery({ queryKey: ['spool-catalog'], queryFn: () => api.getSpoolCatalog() });
  const currency = getCurrencySymbol(settings?.currency || 'USD');

  const [search, setSearch] = useState('');
  const [stockFilter, setStockFilter] = useState<StockFilter>('all');
  const [supplierFilter, setSupplierFilter] = useState('');
  const [brandFilter, setBrandFilter] = useState('');
  const [materialFilter, setMaterialFilter] = useState('');
  const [numberFilter, setNumberFilter] = useState('');
  const [sizeFilter, setSizeFilter] = useState('');
  const [spoolTypeFilter, setSpoolTypeFilter] = useState('');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [columnConfig, setColumnConfig] = useState<ColumnConfig[]>(loadColumns);
  const [showColumns, setShowColumns] = useState(false);
  const [sortState, setSortState] = useState<SortState>(loadSort);

  const catalogMap = useMemo(
    () => Object.fromEntries(catalogEntries.map((entry) => [entry.id, entry])) as Record<number, SpoolCatalogEntry>,
    [catalogEntries],
  );

  // What the filter chips offer: only values some product actually has.
  const options = useMemo(() => {
    const suppliers = new Map<number, string>();
    const brands = new Set<string>();
    const materials = new Set<string>();
    const numbers = new Set<string>();
    const sizes = new Set<number>();
    const spoolTypes = new Set<number>();
    let unsupplied = false;
    let unnumbered = false;
    for (const product of products) {
      if (product.suppliers.length === 0) unsupplied = true;
      for (const supplier of product.suppliers) suppliers.set(supplier.supplier_id, supplier.supplier_name);
      if (product.brand) brands.add(product.brand);
      materials.add(product.material);
      if (product.material_number) numbers.add(product.material_number);
      else unnumbered = true;
      for (const size of product.sizes) {
        sizes.add(size.label_weight);
        if (size.core_weight_catalog_id != null) spoolTypes.add(size.core_weight_catalog_id);
      }
    }
    const byText = (a: string, b: string) => a.localeCompare(b);
    return {
      suppliers: [...suppliers.entries()]
        .map(([id, name]) => ({ id, name }))
        .sort((a, b) => byText(a.name, b.name)),
      brands: [...brands].sort(byText),
      materials: [...materials].sort(byText),
      numbers: [...numbers].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })),
      sizes: [...sizes].sort((a, b) => a - b),
      spoolTypes: [...spoolTypes]
        .filter((id) => catalogMap[id])
        .sort((a, b) => byText(catalogMap[a].name, catalogMap[b].name)),
      unsupplied,
      unnumbered,
    };
  }, [products, catalogMap]);

  const filtered = useMemo(
    () =>
      products.filter((product) => {
        if (!productMatches(product, search)) return false;
        if (stockFilter === 'in_stock' && product.spool_count === 0) return false;
        if (stockFilter === 'empty' && product.spool_count > 0) return false;
        if (stockFilter === 'below' && productTotals(product).below === 0) return false;
        if (supplierFilter === NONE && product.suppliers.length > 0) return false;
        if (
          supplierFilter &&
          supplierFilter !== NONE &&
          !product.suppliers.some((s) => String(s.supplier_id) === supplierFilter)
        ) {
          return false;
        }
        if (brandFilter && product.brand !== brandFilter) return false;
        if (materialFilter && product.material !== materialFilter) return false;
        if (numberFilter === NONE && product.material_number) return false;
        if (numberFilter && numberFilter !== NONE && product.material_number !== numberFilter) return false;
        if (sizeFilter && !product.sizes.some((s) => String(s.label_weight) === sizeFilter)) return false;
        if (spoolTypeFilter && !product.sizes.some((s) => String(s.core_weight_catalog_id) === spoolTypeFilter)) {
          return false;
        }
        return true;
      }),
    [products, search, stockFilter, supplierFilter, brandFilter, materialFilter, numberFilter, sizeFilter, spoolTypeFilter],
  );

  const sorted = useMemo(() => {
    const extractor = sortState ? columnSortValues[sortState.column] : undefined;
    if (!sortState || !extractor) return [...filtered].sort(compareProducts);
    const factor = sortState.direction === 'asc' ? 1 : -1;
    return [...filtered].sort(
      (a, b) => factor * compareSortValues(extractor(a, catalogMap), extractor(b, catalogMap)) || compareProducts(a, b),
    );
  }, [filtered, sortState, catalogMap]);

  const visibleColumns = useMemo(() => columnConfig.filter((c) => c.visible).map((c) => c.id), [columnConfig]);
  // The dialog shows the labels it is given, so it gets them translated —
  // memoised, because it resets its draft whenever the array changes.
  const labelledColumns = useMemo(
    () => columnConfig.map((c) => ({ ...c, label: columnHeaders[c.id]?.(t) ?? c.label })),
    [columnConfig, t],
  );
  const labelledDefaults = useMemo(
    () => DEFAULT_COLUMNS.map((c) => ({ ...c, label: columnHeaders[c.id]?.(t) ?? c.label })),
    [t],
  );

  const hasActiveFilters =
    !!search ||
    stockFilter !== 'all' ||
    !!supplierFilter ||
    !!brandFilter ||
    !!materialFilter ||
    !!numberFilter ||
    !!sizeFilter ||
    !!spoolTypeFilter;

  const clearFilters = () => {
    setSearch('');
    setStockFilter('all');
    setSupplierFilter('');
    setBrandFilter('');
    setMaterialFilter('');
    setNumberFilter('');
    setSizeFilter('');
    setSpoolTypeFilter('');
  };

  const handleSort = (colId: string) => {
    if (!columnSortValues[colId]) return;
    setSortState((prev) => {
      // Ascending, descending, then back to the material-number order.
      const next: SortState =
        prev?.column === colId
          ? prev.direction === 'asc'
            ? { column: colId, direction: 'desc' }
            : null
          : { column: colId, direction: 'asc' };
      saveSort(next);
      return next;
    });
  };

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const shown = useMemo(
    () => ({
      variants: sorted.reduce((sum, p) => sum + p.variants.length, 0),
      spools: sorted.reduce((sum, p) => sum + p.spool_count, 0),
    }),
    [sorted],
  );

  if (isLoading) {
    return (
      <div className="flex justify-center py-16">
        <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
      </div>
    );
  }

  if (products.length === 0) {
    return (
      <div className="bg-bambu-dark-secondary rounded-lg p-8 text-center space-y-3">
        <Boxes className="w-10 h-10 text-bambu-gray mx-auto" />
        <h3 className="text-white font-medium">{t('inventory.products.emptyTitle')}</h3>
        <p className="text-sm text-bambu-gray max-w-xl mx-auto">{t('inventory.products.emptyText')}</p>
        <div className="flex justify-center gap-2 pt-1">
          <button
            onClick={onConvert}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium bg-bambu-green text-white rounded-lg hover:bg-bambu-green/80"
          >
            <WandSparkles className="w-4 h-4" />
            {t('inventory.products.takeOver')}
          </button>
          <button
            onClick={() => onEdit(null)}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary hover:text-white"
          >
            <Plus className="w-4 h-4" />
            {t('inventory.products.newProduct')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {unassignedCount > 0 && (
        <div className="flex flex-wrap items-center gap-3 px-4 py-2.5 rounded-lg bg-amber-500/10 border border-amber-500/30">
          <span className="text-sm text-amber-200 flex-1 min-w-[12rem]">
            {t('inventory.products.unassignedBanner', { count: unassignedCount })}
          </span>
          <button
            onClick={onConvert}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-amber-100 border border-amber-500/40 rounded-lg hover:bg-amber-500/20"
          >
            <WandSparkles className="w-4 h-4" />
            {t('inventory.products.takeOver')}
          </button>
        </div>
      )}

      {/* Toolbar: search, and the columns like the spool list has them */}
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
        <button
          onClick={() => setShowColumns(true)}
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium text-bambu-gray border border-bambu-dark-tertiary rounded-lg hover:bg-bambu-dark-tertiary transition-colors"
          title={t('inventory.configureColumns')}
        >
          <Columns className="w-4 h-4" />
          <span className="hidden sm:inline">{t('inventory.columns')}</span>
        </button>
      </div>

      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center rounded-lg border border-bambu-dark-tertiary overflow-hidden">
          <button onClick={() => setStockFilter('all')} className={segmentClass(stockFilter === 'all')}>
            {t('inventory.products.filters.all')}
          </button>
          <button onClick={() => setStockFilter('in_stock')} className={segmentClass(stockFilter === 'in_stock')}>
            {t('inventory.products.filters.inStock')}
          </button>
          <button onClick={() => setStockFilter('empty')} className={segmentClass(stockFilter === 'empty')}>
            {t('inventory.products.filters.empty')}
          </button>
          <button onClick={() => setStockFilter('below')} className={segmentClass(stockFilter === 'below', 'red')}>
            {t('inventory.products.filters.below')}
          </button>
        </div>

        <div className="w-px h-5 bg-bambu-dark-tertiary" />

        {(options.suppliers.length > 0 || supplierFilter) && (
          <select
            value={supplierFilter}
            onChange={(e) => setSupplierFilter(e.target.value)}
            className={chipClass(!!supplierFilter)}
            aria-label={t('inventory.products.filters.supplier')}
          >
            <option value="">{t('inventory.products.filters.supplier')}</option>
            {options.suppliers.map((s) => (
              <option key={s.id} value={String(s.id)}>
                {s.name}
              </option>
            ))}
            {options.unsupplied && <option value={NONE}>{t('inventory.products.filters.noSupplier')}</option>}
          </select>
        )}
        {(options.brands.length > 0 || brandFilter) && (
          <select
            value={brandFilter}
            onChange={(e) => setBrandFilter(e.target.value)}
            className={chipClass(!!brandFilter)}
            aria-label={t('inventory.products.filters.brand')}
          >
            <option value="">{t('inventory.products.filters.brand')}</option>
            {options.brands.map((b) => (
              <option key={b} value={b}>
                {b}
              </option>
            ))}
          </select>
        )}
        <select
          value={materialFilter}
          onChange={(e) => setMaterialFilter(e.target.value)}
          className={chipClass(!!materialFilter)}
          aria-label={t('inventory.products.filters.material')}
        >
          <option value="">{t('inventory.products.filters.material')}</option>
          {options.materials.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        {(options.numbers.length > 0 || numberFilter) && (
          <select
            value={numberFilter}
            onChange={(e) => setNumberFilter(e.target.value)}
            className={chipClass(!!numberFilter)}
            aria-label={t('inventory.products.filters.materialNumber')}
          >
            <option value="">{t('inventory.products.filters.materialNumber')}</option>
            {options.numbers.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
            {options.unnumbered && <option value={NONE}>{t('inventory.products.filters.noNumber')}</option>}
          </select>
        )}
        {(options.sizes.length > 1 || sizeFilter) && (
          <select
            value={sizeFilter}
            onChange={(e) => setSizeFilter(e.target.value)}
            className={chipClass(!!sizeFilter)}
            aria-label={t('inventory.products.filters.size')}
          >
            <option value="">{t('inventory.products.filters.size')}</option>
            {options.sizes.map((grams) => (
              <option key={grams} value={String(grams)}>
                {formatWeight(grams)}
              </option>
            ))}
          </select>
        )}
        {(options.spoolTypes.length > 0 || spoolTypeFilter) && (
          <select
            value={spoolTypeFilter}
            onChange={(e) => setSpoolTypeFilter(e.target.value)}
            className={chipClass(!!spoolTypeFilter)}
            aria-label={t('inventory.products.filters.spoolType')}
          >
            <option value="">{t('inventory.products.filters.spoolType')}</option>
            {options.spoolTypes.map((id) => (
              <option key={id} value={String(id)}>
                {catalogMap[id].name}
              </option>
            ))}
          </select>
        )}

        {hasActiveFilters && (
          <>
            <div className="w-px h-5 bg-bambu-dark-tertiary" />
            <button
              onClick={clearFilters}
              className="flex items-center gap-1 text-xs text-bambu-gray hover:text-bambu-green transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              {t('inventory.clearFilters')}
            </button>
          </>
        )}

        <span className="ml-auto text-xs text-bambu-gray">
          {t('inventory.products.count', { count: sorted.length })}
        </span>
      </div>

      {sorted.length === 0 ? (
        <div className="bg-bambu-dark-secondary rounded-lg p-8 text-center text-sm text-bambu-gray">
          {t('inventory.products.noMatch')}
        </div>
      ) : (
        <div className="bg-bambu-dark-secondary rounded-lg overflow-hidden border border-bambu-dark-tertiary">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-bambu-dark-tertiary bg-bambu-dark-tertiary/30">
                  <th className="w-10 px-3 py-3" />
                  {visibleColumns.map((colId) => {
                    const sortable = !!columnSortValues[colId];
                    const isActive = sortState?.column === colId;
                    return (
                      <th
                        key={colId}
                        className={`text-left py-3 px-4 text-xs font-medium uppercase tracking-wide select-none whitespace-nowrap ${
                          sortable ? 'cursor-pointer hover:text-bambu-green transition-colors' : ''
                        } ${isActive ? 'text-bambu-green' : 'text-bambu-gray'}`}
                        onClick={sortable ? () => handleSort(colId) : undefined}
                      >
                        <span className="inline-flex items-center gap-1">
                          {columnHeaders[colId]?.(t) ?? colId}
                          {sortable &&
                            (isActive ? (
                              sortState.direction === 'asc' ? (
                                <ArrowUp className="w-3 h-3" />
                              ) : (
                                <ArrowDown className="w-3 h-3" />
                              )
                            ) : (
                              <ArrowUpDown className="w-3 h-3 opacity-30" />
                            ))}
                        </span>
                      </th>
                    );
                  })}
                  <th className="text-right py-3 px-4 text-xs font-medium text-bambu-gray uppercase tracking-wide">
                    {t('common.actions')}
                  </th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((product) => {
                  const open = expanded.has(product.id);
                  return (
                    <Fragment key={product.id}>
                      <tr
                        className="border-b border-bambu-dark-tertiary/50 hover:bg-bambu-dark-tertiary/30 transition-colors cursor-pointer"
                        onClick={() => toggle(product.id)}
                        aria-expanded={open}
                      >
                        <td className="w-10 px-3 py-3 text-bambu-gray">
                          {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                        </td>
                        {visibleColumns.map((colId) => (
                          <td key={colId} className="py-3 px-4">
                            {columnCells[colId]?.({ product, t, currency, catalogMap })}
                          </td>
                        ))}
                        <td className="py-3 px-4">
                          <div className="flex items-center justify-end gap-1" onClick={(e) => e.stopPropagation()}>
                            <button
                              onClick={() => onIntake(product.id)}
                              className="p-1.5 text-bambu-gray hover:text-white rounded transition-colors"
                              title={t('inventory.products.intakeTitle')}
                              aria-label={t('inventory.products.intakeTitle')}
                            >
                              <PackagePlus className="w-4 h-4" />
                            </button>
                            <button
                              onClick={() => onEdit(product)}
                              className="p-1.5 text-bambu-gray hover:text-white rounded transition-colors"
                              title={t('common.edit')}
                              aria-label={t('common.edit')}
                            >
                              <Pencil className="w-4 h-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                      {open && (
                        <tr className="bg-bambu-dark/40 border-b border-bambu-dark-tertiary/50">
                          <td colSpan={visibleColumns.length + 2} className="px-6 py-3">
                            <StockMatrix product={product} currency={currency} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="px-4 py-3 bg-bambu-dark-tertiary/50 border-t border-bambu-dark-tertiary text-sm text-bambu-gray">
            {t('inventory.products.summary', {
              products: sorted.length,
              variants: shown.variants,
              spools: shown.spools,
            })}
          </div>
        </div>
      )}

      <ColumnConfigModal
        isOpen={showColumns}
        onClose={() => setShowColumns(false)}
        columns={labelledColumns}
        defaultColumns={labelledDefaults}
        onSave={(config) => {
          setColumnConfig(config);
          saveColumns(config);
        }}
      />
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
                    {variant.min_stock !== null && (
                      <span
                        className={`block text-[10px] ${variant.shortfall > 0 ? 'text-red-400' : 'text-bambu-gray'}`}
                      >
                        {[
                          t('inventory.products.targetShort', { count: variant.min_stock }),
                          variant.spool_count > variant.in_stock
                            ? t('inventory.products.lowShort', { count: variant.spool_count - variant.in_stock })
                            : null,
                          variant.on_order > 0 ? t('inventory.products.onOrderShort', { count: variant.on_order }) : null,
                        ]
                          .filter(Boolean)
                          .join(' · ')}
                      </span>
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
            .map((s) => `${s.supplier_name}${s.preferred ? ' ★' : ''}`)
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
