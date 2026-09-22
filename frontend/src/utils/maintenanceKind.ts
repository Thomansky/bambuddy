/**
 * Manual versus automated maintenance (#3127).
 *
 * The page mixes two kinds of item: reminders a person performs and ticks
 * off, and tasks Bambuddy runs itself because the type carries an `action`.
 * One classifier for both surfaces -- the printer cards, which know the
 * item's trigger, and the types tab, which only knows the type -- so the
 * badge, the per-printer counts and the filter can never disagree.
 */

import type { MaintenanceAction, MaintenanceTriggerMode } from '../api/client';

export type MaintenanceKind = 'automatic' | 'onRequest' | 'manual';

/** The filter on the status tab; it splits on "can Bambuddy do it". */
export type MaintenanceKindFilter = 'all' | 'automatic' | 'manual';

export const MAINTENANCE_KIND_FILTERS: MaintenanceKindFilter[] = ['all', 'automatic', 'manual'];

/**
 * Which kind an item (or, without a trigger, a type) is.
 *
 * An actionable item on the `manual` trigger still runs by itself once
 * asked, so it reads "Runs on request" rather than "Manual" -- a type, which
 * has no trigger of its own, is simply automatable or not.
 */
export function maintenanceKind(
  action: MaintenanceAction | string | null | undefined,
  triggerMode?: MaintenanceTriggerMode | string | null
): MaintenanceKind {
  if (!action) return 'manual';
  return triggerMode === 'manual' ? 'onRequest' : 'automatic';
}

/** Does Bambuddy perform this one? What the counts and the filter split on. */
export function isAutomatedMaintenance(action: MaintenanceAction | string | null | undefined): boolean {
  return Boolean(action);
}

export function matchesKindFilter(
  action: MaintenanceAction | string | null | undefined,
  filter: MaintenanceKindFilter
): boolean {
  if (filter === 'all') return true;
  return filter === 'automatic' ? isAutomatedMaintenance(action) : !isAutomatedMaintenance(action);
}

export const MAINTENANCE_KIND_LABEL_KEYS: Record<MaintenanceKind, string> = {
  automatic: 'maintenance.kind.automatic',
  onRequest: 'maintenance.kind.onRequest',
  manual: 'maintenance.kind.manual',
};

export const MAINTENANCE_KIND_BADGE_CLASS: Record<MaintenanceKind, string> = {
  automatic: 'bg-bambu-green/20 text-bambu-green',
  onRequest: 'bg-bambu-green/10 text-bambu-green/80',
  manual: 'bg-bambu-dark text-bambu-gray',
};
