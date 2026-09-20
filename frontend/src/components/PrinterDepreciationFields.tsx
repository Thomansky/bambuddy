import { useTranslation } from 'react-i18next';

import { deriveHourlyRate, type DepreciationFormValues } from '../utils/depreciation';

// Printer depreciation inputs (#694), shared by the Add and Edit printer
// dialogs. The price input carries the configured currency symbol and the
// help line turns into the derived hourly rate as soon as both fields hold
// positive numbers.

export function PrinterDepreciationFields({
  values,
  onChange,
  currencySymbol,
  idPrefix,
}: {
  values: DepreciationFormValues;
  onChange: (values: DepreciationFormValues) => void;
  currencySymbol: string;
  idPrefix: string;
}) {
  const { t } = useTranslation();
  const rate = deriveHourlyRate(values);
  const inputClass =
    'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label htmlFor={`${idPrefix}_purchase_price`} className="block text-sm text-bambu-gray mb-1">
            {t('printers.modal.purchasePrice')}
          </label>
          <div className="relative">
            <input
              id={`${idPrefix}_purchase_price`}
              type="number"
              min="0"
              step="any"
              inputMode="decimal"
              className={`${inputClass} pr-10`}
              value={values.purchase_price}
              onChange={(e) => onChange({ ...values, purchase_price: e.target.value })}
              placeholder="0"
            />
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-sm text-bambu-gray pointer-events-none">
              {currencySymbol}
            </span>
          </div>
        </div>
        <div>
          <label htmlFor={`${idPrefix}_expected_lifetime_hours`} className="block text-sm text-bambu-gray mb-1">
            {t('printers.modal.expectedLifetimeHours')}
          </label>
          <input
            id={`${idPrefix}_expected_lifetime_hours`}
            type="number"
            min="0"
            step="any"
            inputMode="decimal"
            className={inputClass}
            value={values.expected_lifetime_hours}
            onChange={(e) => onChange({ ...values, expected_lifetime_hours: e.target.value })}
            placeholder="0"
          />
        </div>
      </div>
      <p className="text-xs text-bambu-gray">
        {rate != null
          ? t('printers.modal.depreciationRate', { rate: rate.toFixed(2), currency: currencySymbol })
          : t('printers.modal.depreciationHelp')}
      </p>
    </div>
  );
}
