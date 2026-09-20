import type { AppSettings } from '../api/client';

// The VAT working basis (settings.price_vat_basis) is the single basis every
// calculated amount is in once vat_enabled is on: spool prices are
// normalised to it server-side, every other price input is entered in it.
// So a displayed amount gets the same suffix everywhere — and none at all
// while the distinction is off.
export type VatSettings = Pick<AppSettings, 'vat_enabled' | 'price_vat_basis'>;

type TFn = (key: string) => string;

export function vatSuffix(settings: Partial<VatSettings> | null | undefined, t: TFn): string {
  if (!settings?.vat_enabled) return '';
  return settings.price_vat_basis === 'net' ? t('common.vatExcl') : t('common.vatIncl');
}
