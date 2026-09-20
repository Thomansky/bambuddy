import { useTranslation } from 'react-i18next';

// Printer wear rate input (#694), shared by the Add and Edit printer dialogs.
// One number per printing hour in the configured currency; the backend caps
// it at 4 decimals, hence the step.

export function PrinterDepreciationFields({
  value,
  onChange,
  currencySymbol,
  idPrefix,
}: {
  value: string;
  onChange: (value: string) => void;
  currencySymbol: string;
  idPrefix: string;
}) {
  const { t } = useTranslation();

  return (
    <div>
      <label htmlFor={`${idPrefix}_wear_cost_per_hour`} className="block text-sm text-bambu-gray mb-1">
        {t('printers.modal.wearCostPerHour')}
      </label>
      <div className="relative">
        <input
          id={`${idPrefix}_wear_cost_per_hour`}
          type="number"
          min="0"
          step="0.0001"
          inputMode="decimal"
          className="w-full px-3 py-2 pr-12 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="0.00"
        />
        <span className="absolute right-3 top-1/2 -translate-y-1/2 text-sm text-bambu-gray pointer-events-none">
          {currencySymbol}
        </span>
      </div>
      <p className="mt-1 text-xs text-bambu-gray">{t('printers.modal.wearCostPerHourHelp')}</p>
    </div>
  );
}
