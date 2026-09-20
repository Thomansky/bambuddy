// Printer depreciation form helpers (#694). Inputs are kept as strings so an
// empty field stays empty instead of collapsing to 0 — both empty means "off",
// and 0 would be ambiguous.

export interface DepreciationFormValues {
  purchase_price: string;
  expected_lifetime_hours: string;
}

/** Hourly wear rate, or null unless both inputs are positive numbers. */
export function deriveHourlyRate(values: DepreciationFormValues): number | null {
  const price = parseFloat(values.purchase_price);
  const hours = parseFloat(values.expected_lifetime_hours);
  if (!Number.isFinite(price) || !Number.isFinite(hours) || price <= 0 || hours <= 0) return null;
  return price / hours;
}

/** Form string -> API value: empty is null, anything else a number. */
export function depreciationFieldToApi(value: string): number | null {
  const trimmed = value.trim();
  if (trimmed === '') return null;
  const parsed = parseFloat(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

export function depreciationFieldFromApi(value: number | null | undefined): string {
  return value == null ? '' : String(value);
}
