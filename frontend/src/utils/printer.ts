export function getPrinterImage(model: string | null | undefined): string {
  if (!model) return '/img/printers/default.png';
  const m = model.toLowerCase().replace(/\s+/g, '');
  if (m.includes('x1e')) return '/img/printers/x1e.png';
  if (m.includes('x1c') || m.includes('x1carbon')) return '/img/printers/x1c.png';
  if (m.includes('x1')) return '/img/printers/x1c.png';
  if (m.includes('x2d') || m === 'n6') return '/img/printers/x2d.png';
  if (m.includes('h2dpro') || m.includes('h2d-pro')) return '/img/printers/h2dpro.png';
  if (m.includes('h2d')) return '/img/printers/h2d.png';
  if (m.includes('h2c')) return '/img/printers/h2c.png';
  if (m.includes('h2s')) return '/img/printers/h2d.png';
  if (m.includes('p2s')) return '/img/printers/p1s.png';
  if (m.includes('p1s')) return '/img/printers/p1s.png';
  if (m.includes('p1p')) return '/img/printers/p1p.png';
  if (m.includes('a2l') || m === 'n9') return '/img/printers/a2l.png';
  if (m.includes('a1mini')) return '/img/printers/a1mini.png';
  if (m.includes('a1')) return '/img/printers/a1.png';
  return '/img/printers/default.png';
}

// Ceiling for every chamber-temperature target the UI accepts (manual set,
// preheat filament map, per-item preheat override, chamber quick-select
// presets). Mirrors backend MAX_CHAMBER_TEMP_C in
// backend/app/utils/printer_models.py — keep the two in sync. The H2 series
// (H2C / H2D / H2D Pro / H2S) and X2D heat the chamber to 65 °C; X1E tops out
// at 60 and its firmware clamps anything higher.
export const MAX_CHAMBER_TEMP_C = 65;

// G-code interchange families (#2578). Mirrors backend GCODE_COMPAT_FAMILIES
// in backend/app/utils/printer_models.py — keep the two in sync. A sliced 3MF
// may target a different model ONLY within its family; everything else is
// exact-match only.
const GCODE_COMPAT_FAMILIES: ReadonlyArray<ReadonlySet<string>> = [
  new Set(['X1', 'X1C', 'X1E', 'P1P', 'P1S']),
];

/** True when G-code sliced for one model may be dispatched to the other.
 *  Unknown/missing metadata on either side returns true (can't validate). */
export function isGcodeCompatible(
  slicedForModel: string | null | undefined,
  targetModel: string | null | undefined,
): boolean {
  if (!slicedForModel || !targetModel) return true;
  const norm = (m: string) => m.trim().toUpperCase().replace(/[\s-]/g, '');
  const a = norm(slicedForModel);
  const b = norm(targetModel);
  if (a === b) return true;
  return GCODE_COMPAT_FAMILIES.some((family) => family.has(a) && family.has(b));
}

// SSDP / MQTT model code -> the display name Bambuddy stores in printers.model.
//
// This is what LAN discovery feeds the add-printer form, so the value it
// returns is the one most printers are saved under — and every backend gate
// that keys on the model (Micro Lidar, rod type, ethernet, G-code
// interchange) reads that saved value. Two entries were previously inverted
// and one was plain wrong: C11 is a P1P and C12 a P1S, and C13 is the X1E,
// not a P2S. firmware_check, the virtual printer's manager and mqtt_server,
// the vp_model_fixes migration, MODEL_SERIAL_PREFIXES and camera.py all
// agree on all three. A C13 saved as "P2S" lost the Micro Lidar box, was
// offered steel-rod maintenance instead of carbon, and had X1-sliced files
// refused.
//
// Keep in sync with backend PRINTER_MODEL_ID_MAP in
// backend/app/utils/printer_models.py.
const SSDP_MODEL_CODES: Readonly<Record<string, string>> = {
  // H2 Series
  'O1D': 'H2D',
  'O1E': 'H2D Pro',
  'O2D': 'H2D Pro',
  'O1C': 'H2C',
  'O1C2': 'H2C',
  'O1S': 'H2S',
  // X1 Series
  'BL-P001': 'X1C',
  'BL-P002': 'X1',
  'BL-P003': 'X1E',
  'C13': 'X1E',
  // X2 Series
  'N6': 'X2D',
  // A2 Series
  'N9': 'A2L',
  // P Series
  'C11': 'P1P',
  'C12': 'P1S',
  'N7': 'P2S',
  // A1 Series
  'N2S': 'A1',
  'N1': 'A1 Mini',
  // Direct matches — a printer that already reports its display name
  'X1C': 'X1C',
  'X1': 'X1',
  'X1E': 'X1E',
  'X2D': 'X2D',
  'P1S': 'P1S',
  'P1P': 'P1P',
  'P2S': 'P2S',
  'A1': 'A1',
  'A1 Mini': 'A1 Mini',
  'A2L': 'A2L',
  'H2D': 'H2D',
  'H2D Pro': 'H2D Pro',
  'H2C': 'H2C',
  'H2S': 'H2S',
};

/** Display name for an SSDP/MQTT model code; unknown codes pass through. */
export function mapModelCode(ssdpModel: string | null | undefined): string {
  if (!ssdpModel) return '';
  return SSDP_MODEL_CODES[ssdpModel] || ssdpModel;
}

export function getWifiStrength(rssi: number): { labelKey: string; color: string; bars: number } {
  if (rssi >= -50) return { labelKey: 'printers.wifiSignal.excellent', color: 'text-bambu-green', bars: 4 };
  if (rssi >= -60) return { labelKey: 'printers.wifiSignal.good', color: 'text-bambu-green', bars: 3 };
  if (rssi >= -70) return { labelKey: 'printers.wifiSignal.fair', color: 'text-yellow-400', bars: 2 };
  if (rssi >= -80) return { labelKey: 'printers.wifiSignal.weak', color: 'text-orange-400', bars: 1 };
  return { labelKey: 'printers.wifiSignal.veryWeak', color: 'text-red-400', bars: 1 };
}

import type { PrinterStatus, PrintQueueItem } from '../api/client';

/**
 * True when a queue item aimed at this printer would start now rather than wait.
 *
 * Every print Bambuddy sends goes through the queue, so this is not "can we
 * print at all" — it is "will ASAP mean now". The PrintModal uses it to promise
 * a later start, and the printer card uses it to say whether a dropped file
 * prints or queues. Both must agree, or the card promises one thing and the
 * modal immediately says another.
 */
export function isPrinterCurrentlyDispatchable(status: PrinterStatus | undefined): boolean {
  if (!status?.connected) return false;
  if (status.awaiting_plate_clear) return false;
  if (status.ams?.some((ams) => ams.dry_time > 0)) return false;
  return ['IDLE', 'FINISH', 'FAILED'].includes(status.state ?? '');
}

/**
 * Filters queue items based on printer compatibility (filament types and colors).
 * Mirrors backend _find_idle_printer_for_model() logic.
 * @param items - Array of queue items to filter
 * @param loadedFilamentTypes - Set of loaded filament types (e.g., "PLA", "PETG")
 * @param loadedFilaments - Set of loaded filament type+color pairs (e.g., "PLA:ffffff", "PETG:ff0000")
 * @param loadedVariants - Set of loaded type+color+tray_info_idx triples
 *   (e.g., "PLA:ffffff:GFA01"; the idx is "" for custom/third-party spools). Used to
 *   distinguish Bambu PLA sub-variants (Basic GFA00 / Matte GFA01 / Silk GFA06) that
 *   share a base type+colour, mirroring the backend _get_missing_force_color_slots (#2650).
 *   When omitted, force matching falls back to type+colour so the hint is never stricter
 *   than the data available.
 * @returns Array of compatible queue items
 */
export function filterCompatibleQueueItems(
  items: PrintQueueItem[],
  loadedFilamentTypes?: Set<string>,
  loadedFilaments?: Set<string>,
  loadedVariants?: Set<string>
): PrintQueueItem[] {
  return items.filter(item => {
    // A job bound to one printer has no printer left to choose, and the backend
    // never gates it on filament: the scheduler maps its trays at dispatch. Such
    // a job can carry overrides — one moved from "Any P2S" to a specific P2S
    // keeps its colour (#3133) — and filtering on them would hide a job that is
    // going to run from the card of the printer it is going to run on.
    if (item.printer_id != null) return true;

    // Type check: all required filament types must be loaded
    if (item.required_filament_types && item.required_filament_types.length > 0 && loadedFilamentTypes !== undefined) {
      if (!item.required_filament_types.every((t: string) => loadedFilamentTypes.has(t.toUpperCase()))) {
        return false;
      }
    }

    // Color check: evaluate force_color_match per slot
    // Only apply when loadedFilaments is provided (not undefined).
    // An empty Set means no filaments are loaded — force-matched slots cannot match.
    if (item.filament_overrides && item.filament_overrides.length > 0 && loadedFilaments !== undefined) {
      const forceOverrides = item.filament_overrides.filter(o => o.force_color_match === true);
      const prefOverrides = item.filament_overrides.filter(o => o.force_color_match !== true);

      // All force-matched slots must have an exact type+color match — and, when the
      // override carries a tray_info_idx, the same variant too (a loaded tray with a
      // blank idx still satisfies it, matching the backend's type+colour fallback).
      if (forceOverrides.length > 0) {
        const allForceMatch = forceOverrides.every(o => {
          const oType = (o.type || '').toUpperCase();
          const oColor = (o.color || '').replace('#', '').toLowerCase().slice(0, 6);
          const oIdx = o.tray_info_idx || '';
          // No variant on the override, or no variant data supplied → type+colour only.
          if (!oIdx || loadedVariants === undefined) {
            return loadedFilaments.has(`${oType}:${oColor}`);
          }
          // Variant-specific: same idx, or a same-colour tray that reports no idx.
          return loadedVariants.has(`${oType}:${oColor}:${oIdx}`) || loadedVariants.has(`${oType}:${oColor}:`);
        });
        if (!allForceMatch) return false;
      }

      // Preference-only overrides: at least one color must match (existing behaviour)
      if (prefOverrides.length > 0 && forceOverrides.length === 0) {
        const hasColorMatch = prefOverrides.some(o => {
          const oType = (o.type || '').toUpperCase();
          const oColor = (o.color || '').replace('#', '').toLowerCase().slice(0, 6);
          return loadedFilaments.has(`${oType}:${oColor}`);
        });
        if (!hasColorMatch) return false;
      }
    }

    return true;
  });
}
