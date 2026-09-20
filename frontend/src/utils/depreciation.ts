// Printer wear-rate form helpers (#694). The input is kept as a string so an
// empty field stays empty instead of collapsing to 0.

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
