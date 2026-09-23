/**
 * Flow-dynamics (pressure advance) calibration, per AMS slot.
 *
 * The measurement is a real print: the printer lays a 30 mm line and runs the
 * vendor's own calibration block in its start G-code. That makes this modal a
 * confirmation dialog first and a settings form second — it has to say what
 * will physically happen, what it will cost in time and filament, and, when it
 * cannot start, exactly why.
 *
 * Every blocking reason comes from the backend's preflight. Start is never a
 * silently dead button: if it is disabled there is a sentence next to it.
 */
import { useEffect, useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { X, AlertTriangle, Gauge } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { api } from '../api/client';
import type { PaPresetRef, PaPresetTriplet } from '../api/client';

interface PaCalibrationModalProps {
  isOpen: boolean;
  printerId: number;
  printerName: string;
  amsId: number;
  slotId: number;
  slotLabel: string;
  onClose: () => void;
  /** Called once a run has been created, so the page can show its card. */
  onStarted?: (runId: number) => void;
}

function formatK(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(3);
}

export function PaCalibrationModal({
  isOpen,
  printerId,
  printerName,
  amsId,
  slotId,
  slotLabel,
  onClose,
  onStarted,
}: PaCalibrationModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [plateType, setPlateType] = useState<string>('');
  const [plateConfirmed, setPlateConfirmed] = useState(false);
  const [presets, setPresets] = useState<PaPresetTriplet | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data: preflight, isLoading } = useQuery({
    queryKey: ['pa-calibration-preflight', printerId, amsId, slotId],
    queryFn: () => api.getPaCalibrationPreflight(printerId, amsId, slotId),
    enabled: isOpen,
    // Short and deliberate: the user is looking at a live decision (is the
    // printer free? is the spool still in?) and a stale "ready" would start a
    // print against a machine that is no longer idle.
    refetchInterval: isOpen ? 10_000 : false,
  });

  useEffect(() => {
    if (!preflight) return;
    setPlateType((current) => current || preflight.default_plate_type || '');
    setPresets((current) => current ?? preflight.presets);
  }, [preflight]);

  // The tick is per opening, never remembered: it is an assertion about the
  // bed right now, not a preference.
  useEffect(() => {
    if (isOpen) {
      setPlateConfirmed(false);
      setError(null);
    }
  }, [isOpen]);

  const startMutation = useMutation({
    mutationFn: () =>
      api.createPaCalibrationRun(printerId, {
        ams_id: amsId,
        slot_id: slotId,
        plate_type: plateType,
        presets: presets as PaPresetTriplet,
        plate_confirmed: plateConfirmed,
      }),
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ['pa-calibration-runs', printerId] });
      onStarted?.(run.id);
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  const blockers = preflight?.blocked_reasons ?? [];
  const canStart = useMemo(
    () => Boolean(preflight?.supported) && blockers.length === 0 && plateConfirmed && Boolean(presets) && Boolean(plateType),
    [preflight, blockers.length, plateConfirmed, presets, plateType],
  );

  if (!isOpen) return null;

  const modalBg = 'var(--bg-secondary)';
  const sectionBg = 'var(--bg-primary)';
  const borderColor = 'var(--border-color)';
  const textPrimary = 'var(--text-primary)';
  const textSecondary = 'var(--text-secondary)';

  const updatePreset = (slot: keyof PaPresetTriplet, ref: PaPresetRef) => {
    setPresets((current) => (current ? { ...current, [slot]: ref } : current));
  };

  const minutes = Math.ceil((preflight?.estimated_seconds ?? 0) / 60);

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
      onClick={onClose}
      data-testid="pa-calibration-modal"
    >
      <div
        className="rounded-xl w-full max-w-lg max-h-[90vh] overflow-hidden shadow-xl flex flex-col"
        style={{ backgroundColor: modalBg }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="pa-calibration-title"
      >
        <div className="flex items-center justify-between px-5 py-3 border-b" style={{ borderColor }}>
          <h2
            id="pa-calibration-title"
            className="text-base font-semibold flex items-center gap-2"
            style={{ color: textPrimary }}
          >
            <Gauge className="w-4 h-4" />
            {t('paCalibration.title')}
          </h2>
          <button type="button" onClick={onClose} aria-label={t('common.close')} style={{ color: textSecondary }}>
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-5 py-4 space-y-4 overflow-y-auto">
          {isLoading && <p style={{ color: textSecondary }}>{t('common.loading')}</p>}

          {preflight && (
            <>
              <div className="rounded-lg p-3 space-y-1 text-sm" style={{ backgroundColor: sectionBg }}>
                <Row label={t('paCalibration.printer')} value={`${printerName} (${preflight.printer_model ?? '—'})`} />
                <Row
                  label={t('paCalibration.nozzle')}
                  value={`${preflight.nozzle_diameter ?? '—'} mm${preflight.nozzle_id ? ` · ${preflight.nozzle_id}` : ''}`}
                />
                <Row
                  label={t('paCalibration.slot')}
                  value={`${slotLabel}${preflight.filament?.name ? ` · ${preflight.filament.name}` : ''}`}
                />
                <Row
                  label={t('paCalibration.currentK')}
                  value={
                    preflight.current_k === null
                      ? t('paCalibration.noProfileYet')
                      : `${formatK(preflight.current_k)}${
                          preflight.current_profile_name ? ` · ${preflight.current_profile_name}` : ''
                        }`
                  }
                />
                <Row
                  label={t('paCalibration.cost')}
                  value={t('paCalibration.costValue', {
                    minutes,
                    grams: preflight.estimated_grams,
                  })}
                />
              </div>

              {blockers.length > 0 && (
                <div
                  className="rounded-lg p-3 text-sm flex gap-2"
                  style={{ backgroundColor: 'rgba(239,68,68,0.12)', color: 'var(--text-primary)' }}
                  data-testid="pa-calibration-blockers"
                >
                  <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                  <ul className="space-y-1">
                    {blockers.map((reason) => (
                      <li key={reason} data-testid={`pa-blocked-${reason}`}>
                        {t(`paCalibration.blocked.${reason}`, { defaultValue: reason })}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {preflight.supported && (
                <>
                  <label className="block text-sm" style={{ color: textPrimary }}>
                    <span className="block mb-1">{t('paCalibration.plateType')}</span>
                    <select
                      className="w-full rounded px-2 py-1.5 text-sm"
                      style={{ backgroundColor: sectionBg, color: textPrimary, borderColor }}
                      value={plateType}
                      onChange={(e) => setPlateType(e.target.value)}
                      aria-label={t('paCalibration.plateType')}
                    >
                      {preflight.plate_types.map((type) => (
                        <option key={type} value={type}>
                          {t(`paCalibration.plates.${type}`, { defaultValue: type })}
                        </option>
                      ))}
                    </select>
                    <span className="block mt-1 text-xs" style={{ color: textSecondary }}>
                      {t('paCalibration.plateTypeHint')}
                    </span>
                  </label>

                  {presets && (
                    <div className="space-y-2 text-sm" style={{ color: textPrimary }}>
                      <p className="text-xs" style={{ color: textSecondary }}>
                        {t('paCalibration.presetsHint')}
                      </p>
                      {(['printer', 'process', 'filament'] as const).map((slot) => (
                        <label key={slot} className="block">
                          <span className="block mb-1 text-xs" style={{ color: textSecondary }}>
                            {t(`paCalibration.preset.${slot}`)}
                          </span>
                          <input
                            className="w-full rounded px-2 py-1.5 text-sm"
                            style={{ backgroundColor: sectionBg, color: textPrimary, borderColor }}
                            value={presets[slot].id}
                            aria-label={t(`paCalibration.preset.${slot}`)}
                            onChange={(e) =>
                              updatePreset(slot, { source: presets[slot].source, id: e.target.value })
                            }
                          />
                        </label>
                      ))}
                    </div>
                  )}

                  <label className="flex items-start gap-2 text-sm" style={{ color: textPrimary }}>
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={plateConfirmed}
                      onChange={(e) => setPlateConfirmed(e.target.checked)}
                      data-testid="pa-plate-confirm"
                    />
                    <span>{t('paCalibration.plateConfirm')}</span>
                  </label>
                </>
              )}

              {error && (
                <p className="text-sm" style={{ color: 'var(--color-danger, #ef4444)' }} role="alert">
                  {error}
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex justify-end gap-2 px-5 py-3 border-t" style={{ borderColor }}>
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-sm rounded"
            style={{ backgroundColor: sectionBg, color: textPrimary }}
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            onClick={() => startMutation.mutate()}
            disabled={!canStart || startMutation.isPending}
            data-testid="pa-calibration-start"
            className="px-3 py-1.5 text-sm rounded disabled:opacity-40 disabled:cursor-not-allowed"
            style={{ backgroundColor: 'var(--color-primary, #00AE42)', color: '#fff' }}
          >
            {t('paCalibration.start')}
          </button>
        </div>
      </div>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
      <span className="text-right" style={{ color: 'var(--text-primary)' }}>
        {value}
      </span>
    </div>
  );
}
