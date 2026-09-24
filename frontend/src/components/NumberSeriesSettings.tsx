import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Hash, Loader2 } from 'lucide-react';
import { api, renderSeriesNumber, type NumberSeries, type NumberSeriesUpdate } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { useToast } from '../contexts/ToastContext';

const SERIES_LABEL_KEYS: Record<string, string> = {
  project: 'settings.numberSeries.project',
  queue_job: 'settings.numberSeries.queueJob',
  library_folder: 'settings.numberSeries.libraryFolder',
};

/** One series row. Its own component so each keeps its own draft state — the
 *  prefix and the next number are typed, not toggled, and a re-render from the
 *  sibling's save must not throw away what is half-typed here. */
function SeriesRow({ series }: { series: NumberSeries }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const [prefix, setPrefix] = useState(series.prefix);
  const [suffix, setSuffix] = useState(series.suffix);
  const [nextValue, setNextValue] = useState(String(series.next_value));
  const [padding, setPadding] = useState(String(series.padding));

  // Re-seed when the server's copy changes (another tab, a failed save rolled
  // back), but not on every render — a draft the user is typing survives.
  useEffect(() => {
    setPrefix(series.prefix);
    setSuffix(series.suffix);
    setNextValue(String(series.next_value));
    setPadding(String(series.padding));
  }, [series.prefix, series.suffix, series.next_value, series.padding]);

  const save = useMutation({
    mutationFn: (data: NumberSeriesUpdate) => api.updateNumberSeries(series.key, data),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['number-series'] }),
    onError: () => {
      showToast(t('settings.numberSeries.saveError'), 'error');
      queryClient.invalidateQueries({ queryKey: ['number-series'] });
    },
  });

  const parsedNext = Number.parseInt(nextValue, 10);
  const parsedPadding = Number.parseInt(padding, 10);
  const effectiveNext = Number.isNaN(parsedNext) ? series.next_value : Math.max(parsedNext, 1);
  const effectivePadding = Number.isNaN(parsedPadding) ? series.padding : Math.max(parsedPadding, 0);
  // Rendered locally so the preview follows the field the user is still typing.
  // Same formula as the backend's `render_number`, which is what will actually
  // be stored.
  const preview = renderSeriesNumber(prefix, effectiveNext, effectivePadding, suffix);
  // Only a genuine reduction is worth warning about; typing over the field on
  // the way to a bigger number is not.
  const lowersCounter = effectiveNext < series.next_value;

  const label = t(SERIES_LABEL_KEYS[series.key] ?? series.key);

  return (
    <div className="space-y-2 py-3 border-b border-bambu-dark-tertiary/50 last:border-b-0 last:pb-0">
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-white">{label}</p>
        <label className="relative inline-flex items-center cursor-pointer">
          <input
            type="checkbox"
            checked={series.enabled}
            onChange={(e) => save.mutate({ enabled: e.target.checked })}
            aria-label={`${label} — ${t('settings.numberSeries.enable')}`}
            className="sr-only peer"
          />
          <div className="w-11 h-6 bg-bambu-dark-tertiary peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-bambu-green"></div>
        </label>
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <label className="text-xs text-bambu-gray" htmlFor={`ns-prefix-${series.key}`}>
            {t('settings.numberSeries.prefix')}
          </label>
          <input
            id={`ns-prefix-${series.key}`}
            type="text"
            maxLength={16}
            value={prefix}
            onChange={(e) => setPrefix(e.target.value)}
            onBlur={() => prefix !== series.prefix && save.mutate({ prefix })}
            className="w-24 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-bambu-gray" htmlFor={`ns-next-${series.key}`}>
            {t('settings.numberSeries.nextNumber')}
          </label>
          <input
            id={`ns-next-${series.key}`}
            type="number"
            min={1}
            value={nextValue}
            onChange={(e) => setNextValue(e.target.value)}
            onBlur={() => effectiveNext !== series.next_value && save.mutate({ next_value: effectiveNext })}
            className="w-28 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-bambu-gray" htmlFor={`ns-padding-${series.key}`}>
            {t('settings.numberSeries.padding')}
          </label>
          <input
            id={`ns-padding-${series.key}`}
            type="number"
            min={0}
            max={16}
            value={padding}
            onChange={(e) => setPadding(e.target.value)}
            onBlur={() => effectivePadding !== series.padding && save.mutate({ padding: effectivePadding })}
            className="w-20 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-xs text-bambu-gray" htmlFor={`ns-suffix-${series.key}`}>
            {t('settings.numberSeries.suffix')}
          </label>
          <input
            id={`ns-suffix-${series.key}`}
            type="text"
            maxLength={16}
            value={suffix}
            onChange={(e) => setSuffix(e.target.value)}
            onBlur={() => suffix !== series.suffix && save.mutate({ suffix })}
            className="w-24 px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white text-sm"
          />
        </div>
        <p className="text-xs text-bambu-gray pb-1.5 font-mono">
          {t('settings.numberSeries.preview', { value: preview })}
        </p>
      </div>

      {lowersCounter && (
        <p className="text-xs text-amber-500 dark:text-amber-400">{t('settings.numberSeries.lowerHint')}</p>
      )}
    </div>
  );
}

export function NumberSeriesSettings() {
  const { t } = useTranslation();

  const { data: series, isLoading, error } = useQuery({
    queryKey: ['number-series'],
    queryFn: () => api.getNumberSeries(),
  });

  return (
    <Card id="card-number-series">
      <CardHeader>
        <h3 className="text-base font-semibold text-white flex items-center gap-2">
          <Hash className="w-4 h-4 text-bambu-green" />
          {t('settings.numberSeries.title')}
        </h3>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-bambu-gray">{t('settings.numberSeries.description')}</p>
        {isLoading && <Loader2 className="w-4 h-4 animate-spin text-bambu-green mt-3" />}
        {error && <p className="text-xs text-red-400 mt-3">{t('settings.numberSeries.loadError')}</p>}
        {series?.map((s) => (
          <SeriesRow key={s.key} series={s} />
        ))}
      </CardContent>
    </Card>
  );
}

export default NumberSeriesSettings;
