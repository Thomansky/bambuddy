import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { vatSuffix } from '../utils/vat';

// "incl. VAT" / "excl. VAT" after an amount, in the working basis, or nothing
// while the VAT distinction is off. Reads /settings/ui-flags, not /settings:
// the latter needs SETTINGS_READ, which the non-admin readers of Finance,
// Projects and Archives do not hold, and a 403 would silently drop the label
// while VAT is on (#3023). Layout already holds this query key, so the badge
// is served from cache and every call site stays a one-liner.
export function VatBadge({ className = '' }: { className?: string }) {
  const { t } = useTranslation();
  const { data: uiFlags } = useQuery({ queryKey: ['ui-flags'], queryFn: api.getUiFlags });
  const suffix = vatSuffix(uiFlags, t);
  if (!suffix) return null;
  return <span className={`text-[10px] font-normal text-bambu-gray/70 whitespace-nowrap ${className}`.trim()}> {suffix}</span>;
}
