import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../api/client';
import { vatSuffix } from '../utils/vat';

// "incl. VAT" / "excl. VAT" after an amount, in the working basis, or nothing
// while the VAT distinction is off. Reads the settings query itself (same key
// the pages use, so it is deduplicated) to keep every call site a one-liner.
export function VatBadge({ className = '' }: { className?: string }) {
  const { t } = useTranslation();
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const suffix = vatSuffix(settings, t);
  if (!suffix) return null;
  return <span className={`text-[10px] font-normal text-bambu-gray/70 whitespace-nowrap ${className}`.trim()}> {suffix}</span>;
}
