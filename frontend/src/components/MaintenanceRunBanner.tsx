import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import { Fan, Wrench } from 'lucide-react';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { formatDateTime, formatTimeOnly, parseUTCDate } from '../utils/date';
import { maintenanceTypeLabel } from '../utils/maintenanceTypeLabels';
import { MAINTENANCE_WAITING_REASON_KEYS, nextCalibrationStep } from '../utils/maintenanceRunText';

interface MaintenanceRunBannerProps {
  printerId: number;
  /** The bed temperature the printer reports right now, for a run waiting on it. */
  bedTemp?: number | null;
  timeFormat?: 'system' | '12h' | '24h';
}

const iconClass = 'w-[var(--pc-i3,0.75rem)] h-[var(--pc-i3,0.75rem)] mt-px shrink-0';

// A calibration run (#3127) on the printer card: what runs or waits there,
// what it waits for -- the bed with its live temperature, the fans helping it
// cool -- and what comes next. A click opens the Maintenance page, where the
// run can be cancelled.
export function MaintenanceRunBanner({ printerId, bedTemp, timeFormat = 'system' }: MaintenanceRunBannerProps) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  // One fleet-wide list shared by every card, polled faster while anything runs.
  const { data: runs = [] } = useQuery({
    queryKey: ['maintenance-active-runs'],
    queryFn: api.getActiveMaintenanceRuns,
    enabled: hasPermission('maintenance:read'),
    refetchInterval: (query) => ((query.state.data?.length ?? 0) > 0 ? 10_000 : 60_000),
  });
  const mine = runs.filter((run) => run.printer_id === printerId);
  if (mine.length === 0) return null;
  const [run, ...after] = mine;

  const degrees = (value: number) => value.toLocaleString(i18n.language, { maximumFractionDigits: 1 });
  const name = maintenanceTypeLabel(run.type_name, t);
  const running = run.status === 'running';
  const bed = bedTemp ?? run.waiting_detail?.bed_temp ?? null;
  const cooling = !running && run.waiting_reason === 'bed_too_warm' && run.waiting_detail?.cooling === true;

  const state = (() => {
    if (running) {
      const since = parseUTCDate(run.started_at);
      return since
        ? t('printers.maintenanceRun.runningSince', { time: formatTimeOnly(since, timeFormat) })
        : t('printers.maintenanceRun.running');
    }
    const startAfter = parseUTCDate(run.start_after);
    if (startAfter && startAfter.getTime() > Date.now()) {
      return t('printers.maintenanceRun.startsAt', { time: formatDateTime(run.start_after, timeFormat) });
    }
    if (run.waiting_reason === 'bed_too_warm' && run.bed_temp_below != null && bed != null) {
      return t('printers.maintenanceRun.bedCooling', { temp: degrees(bed), target: degrees(run.bed_temp_below) });
    }
    if (run.waiting_reason) {
      const key = MAINTENANCE_WAITING_REASON_KEYS[run.waiting_reason];
      return key
        ? t(key, {
            temp: bed != null ? degrees(bed) : '?',
            item: maintenanceTypeLabel(run.waiting_detail?.item ?? '', t),
          })
        : t('maintenance.calibration.waitingOther', { reason: run.waiting_reason });
    }
    return t('maintenance.calibration.runQueued');
  })();

  const stepText = (step: ReturnType<typeof nextCalibrationStep>): string => {
    if (step === 'visionPlate') return t('printers.maintenanceRun.stepVisionPlate');
    if (step === 'coolBed') {
      const target = degrees(run.bed_temp_below ?? 0);
      return run.assisted_cooling
        ? t('printers.maintenanceRun.stepCoolBedFans', { target })
        : t('printers.maintenanceRun.stepCoolBed', { target });
    }
    return t('printers.maintenanceRun.stepStart');
  };

  const lines: string[] = [];
  if (cooling) lines.push(t('maintenance.calibration.fansRunning'));
  if (running && run.stage) {
    lines.push(
      run.stage.index != null && run.stage.count != null
        ? t('printers.maintenanceRun.stage', { index: run.stage.index, count: run.stage.count, stage: run.stage.current })
        : t('printers.maintenanceRun.stageOnly', { stage: run.stage.current })
    );
    if (run.stage.next) lines.push(t('printers.maintenanceRun.next', { step: run.stage.next }));
  } else if (!running) {
    lines.push(t('printers.maintenanceRun.next', { step: stepText(nextCalibrationStep(run, bed)) }));
  }
  if (after.length > 0) {
    lines.push(
      t('printers.maintenanceRun.then', { name: after.map((next) => maintenanceTypeLabel(next.type_name, t)).join(', ') })
    );
  }

  return (
    <button
      type="button"
      onClick={() => navigate('/maintenance')}
      title={t('printers.maintenanceRun.open')}
      data-testid="maintenance-run-banner"
      className={`mt-2 w-full text-left px-2 py-1 rounded-lg border text-[length:var(--pc-t11,11px)] leading-snug transition-colors ${
        running
          ? 'bg-bambu-green/10 border-bambu-green/30 hover:bg-bambu-green/15'
          : 'bg-amber-500/10 border-amber-500/30 hover:bg-amber-500/15'
      }`}
    >
      <span className={`flex items-start gap-1.5 ${running ? 'text-bambu-green' : 'text-amber-700 dark:text-amber-400'}`}>
        {cooling ? (
          <Fan className={`${iconClass} motion-safe:animate-[spin_3s_linear_infinite]`} aria-hidden="true" />
        ) : (
          <Wrench className={iconClass} aria-hidden="true" />
        )}
        <span className="min-w-0">
          <span className="font-medium">{name}</span> · {state}
        </span>
      </span>
      {lines.map((line) => (
        <span key={line} className="block pl-[calc(var(--pc-i3,0.75rem)+0.375rem)] text-bambu-gray">
          {line}
        </span>
      ))}
    </button>
  );
}
