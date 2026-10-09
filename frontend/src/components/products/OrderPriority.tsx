import { useTranslation } from 'react-i18next';
import { ArrowDown, Flag } from 'lucide-react';
import type { ProductOrderPriority } from '../../api/client';
import { ORDER_PRIORITIES } from './productUtils';

const CHOICE_STYLE: Record<ProductOrderPriority, string> = {
  high: 'bg-red-500/15 text-red-700 dark:text-red-300',
  normal: 'bg-bambu-dark-tertiary text-white',
  low: 'bg-bambu-dark-tertiary text-bambu-gray',
};

/** High, normal or low as three buttons side by side — the choice when a
 *  line is put on the reorder list. */
export function PriorityChoice({
  value,
  onChange,
  disabled = false,
}: {
  value: ProductOrderPriority;
  onChange: (value: ProductOrderPriority) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div
      role="radiogroup"
      aria-label={t('inventory.products.orders.priority.label')}
      className="inline-flex rounded-lg border border-bambu-dark-tertiary overflow-hidden"
    >
      {ORDER_PRIORITIES.map((priority) => {
        const checked = value === priority;
        return (
          <button
            key={priority}
            type="button"
            role="radio"
            aria-checked={checked}
            disabled={disabled}
            onClick={() => onChange(priority)}
            className={`px-3 py-1.5 text-sm flex items-center gap-1 transition-colors disabled:opacity-50 ${
              checked ? `${CHOICE_STYLE[priority]} font-medium` : 'text-bambu-gray hover:text-white hover:bg-bambu-dark'
            }`}
          >
            {priority === 'high' && <Flag className="w-3.5 h-3.5" />}
            {priority === 'low' && <ArrowDown className="w-3.5 h-3.5" />}
            {t(`inventory.products.orders.priority.${priority}`)}
          </button>
        );
      })}
    </div>
  );
}

/** The priority on a line, where it is not the usual one: a red flag for
 *  high, a muted mark for low, nothing for normal — so the urgent lines
 *  stand out and the rest stays quiet. */
export function PriorityBadge({ priority }: { priority: ProductOrderPriority }) {
  const { t } = useTranslation();
  if (priority === 'normal') return null;
  const label = t(`inventory.products.orders.priority.${priority}`);
  return priority === 'high' ? (
    <span
      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[11px] font-semibold border border-red-500/40 bg-red-500/15 text-red-700 dark:text-red-300 whitespace-nowrap"
      data-testid="priority-badge"
    >
      <Flag className="w-3 h-3" />
      {label}
    </span>
  ) : (
    <span
      className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[11px] bg-bambu-dark-tertiary text-bambu-gray whitespace-nowrap"
      data-testid="priority-badge"
    >
      <ArrowDown className="w-3 h-3" />
      {label}
    </span>
  );
}
