/**
 * The live card for a flow-dynamics calibration run.
 *
 * State comes from the API, never from component state: the run is a database
 * row precisely so it survives a reload, a backend restart and the user
 * walking away for the seven minutes the print takes.
 *
 * At `awaiting_confirmation` the card stops being a progress indicator and
 * becomes a decision: old K next to new K, whether this overwrites a profile
 * or creates one, and two buttons. Discard makes no call that could write.
 */
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Gauge, X } from 'lucide-react';

import { api } from '../api/client';
import type { PaCalibrationRun } from '../api/client';

interface PaCalibrationCardProps {
  printerId: number;
  /** Rendered instead of polling, so tests and stories can drive the states. */
  run?: PaCalibrationRun | null;
}

const IN_PROGRESS: PaCalibrationRun['status'][] = [
  'queued',
  'slicing',
  'uploading',
  'printing',
  'reading_result',
  'saving',
];

function formatK(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(3);
}

export function PaCalibrationCard({ printerId, run: runProp }: PaCalibrationCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['pa-calibration-runs', printerId],
    queryFn: () => api.getPaCalibrationRuns(printerId, true),
    enabled: runProp === undefined,
    // Fast only while something is actually happening. Most of the time this
    // asks an idle printer whether nothing is still nothing, and every
    // supported printer on the page asks separately.
    refetchInterval: (query) => (query.state.data?.runs?.length ? 5_000 : 20_000),
  });

  const run = runProp ?? data?.runs?.[0] ?? null;

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['pa-calibration-runs', printerId] });

  const confirmMutation = useMutation({
    mutationFn: () => api.confirmPaCalibrationRun(printerId, run!.id),
    onSettled: invalidate,
  });
  const discardMutation = useMutation({
    mutationFn: () => api.discardPaCalibrationRun(printerId, run!.id),
    onSettled: invalidate,
  });
  const cancelMutation = useMutation({
    mutationFn: (stopPrint: boolean) => api.cancelPaCalibrationRun(printerId, run!.id, stopPrint),
    onSettled: invalidate,
  });

  if (!run) return null;

  const sectionBg = 'var(--bg-primary)';
  const borderColor = 'var(--border-color)';
  const textPrimary = 'var(--text-primary)';
  const textSecondary = 'var(--text-secondary)';

  const inProgress = IN_PROGRESS.includes(run.status);
  const awaiting = run.status === 'awaiting_confirmation';
  const overwrites = run.k_before !== null && run.k_before !== undefined;

  return (
    <div
      className="rounded-lg border p-3 space-y-2 text-sm"
      style={{ backgroundColor: sectionBg, borderColor, color: textPrimary }}
      data-testid="pa-calibration-card"
      data-status={run.status}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium flex items-center gap-1.5">
          <Gauge className="w-4 h-4" />
          {t('paCalibration.title')}
        </span>
        <span style={{ color: textSecondary }} data-testid="pa-calibration-stage">
          {t(`paCalibration.status.${run.status}`, { defaultValue: run.status })}
        </span>
      </div>

      <p style={{ color: textSecondary }}>
        {run.filament_name || run.filament_id} · {run.nozzle_diameter} mm
      </p>

      {run.waiting_reason && (
        <p style={{ color: textSecondary }} data-testid="pa-calibration-waiting">
          {t(`paCalibration.waiting.${run.waiting_reason}`, { defaultValue: run.waiting_reason })}
        </p>
      )}

      {inProgress && (run.status === 'slicing' || run.status === 'uploading' || run.status === 'printing') && (
        <div className="h-1.5 rounded overflow-hidden" style={{ backgroundColor: 'var(--border-color)' }}>
          <div
            className="h-full"
            style={{ width: `${Math.round(run.progress)}%`, backgroundColor: 'var(--color-primary, #00AE42)' }}
            data-testid="pa-calibration-progress"
          />
        </div>
      )}

      {awaiting && (
        <div className="space-y-2" data-testid="pa-calibration-result">
          <div className="flex items-center justify-between">
            <span style={{ color: textSecondary }}>{t('paCalibration.oldK')}</span>
            <span>{formatK(run.k_before)}</span>
          </div>
          <div className="flex items-center justify-between font-medium">
            <span style={{ color: textSecondary }}>{t('paCalibration.newK')}</span>
            <span data-testid="pa-calibration-new-k">{formatK(run.k_value)}</span>
          </div>
          <div className="flex items-center justify-between">
            <span style={{ color: textSecondary }}>{t('paCalibration.nCoef')}</span>
            <span>{run.n_coef ?? '—'}</span>
          </div>
          <div className="flex items-center justify-between">
            <span style={{ color: textSecondary }}>{t('paCalibration.confidence')}</span>
            <span>{run.confidence ?? '—'}</span>
          </div>
          <p style={{ color: textSecondary }} data-testid="pa-calibration-write-mode">
            {overwrites ? t('paCalibration.willOverwrite') : t('paCalibration.willCreate')}
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => confirmMutation.mutate()}
              disabled={confirmMutation.isPending}
              data-testid="pa-calibration-confirm"
              className="flex-1 px-3 py-1.5 rounded text-sm disabled:opacity-40"
              style={{ backgroundColor: 'var(--color-primary, #00AE42)', color: '#fff' }}
            >
              {t('paCalibration.save')}
            </button>
            <button
              type="button"
              onClick={() => discardMutation.mutate()}
              disabled={discardMutation.isPending}
              data-testid="pa-calibration-discard"
              className="flex-1 px-3 py-1.5 rounded text-sm disabled:opacity-40"
              style={{ backgroundColor: 'var(--bg-secondary)', color: textPrimary }}
            >
              {t('paCalibration.discard')}
            </button>
          </div>
        </div>
      )}

      {run.status === 'failed' && run.error_message && (
        <p style={{ color: 'var(--color-danger, #ef4444)' }} data-testid="pa-calibration-error">
          {run.error_message}
        </p>
      )}

      {inProgress && (
        <button
          type="button"
          onClick={() => {
            // Stopping a running print is not something to do on an ambiguous
            // click, so the printing case asks first and then passes the flag
            // the route requires.
            if (run.status === 'printing' && !window.confirm(t('paCalibration.cancelPrintConfirm'))) return;
            cancelMutation.mutate(run.status === 'printing');
          }}
          disabled={cancelMutation.isPending}
          data-testid="pa-calibration-cancel"
          className="flex items-center gap-1 text-xs"
          style={{ color: textSecondary }}
        >
          <X className="w-3 h-3" />
          {t('common.cancel')}
        </button>
      )}
    </div>
  );
}
