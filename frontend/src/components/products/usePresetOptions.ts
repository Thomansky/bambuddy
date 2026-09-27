import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';
import type { SlicerSetting } from '../../api/client';
import { buildFilamentOptions } from '../spool-form/utils';
import type { FilamentOption } from '../spool-form/types';

const FIVE_MINUTES = 5 * 60 * 1000;

/**
 * The slicer presets the spool dialog offers — Bambu Cloud, Orca Cloud,
 * imported and built-in — as options for a product's preset (#3165).
 *
 * The same sources and the same builder as the spool dialog, so a product
 * lists exactly the presets a spool of it could be given. Cached for a few
 * minutes: the editor opens and closes often while products are set up.
 */
export function usePresetOptions(): { options: FilamentOption[]; loading: boolean } {
  const cloud = useQuery({
    queryKey: ['product-editor-cloud-presets'],
    queryFn: async () => {
      const [bambu, orca] = await Promise.allSettled([
        (async () => {
          const status = await api.getCloudStatus();
          return status.is_authenticated ? await api.getFilamentPresets() : ([] as SlicerSetting[]);
        })(),
        (async () => {
          const status = await api.orcaCloudStatus();
          if (!status.connected) return [] as SlicerSetting[];
          // OrcaProfileMeta is structurally identical to SlicerSetting.
          return (await api.orcaCloudListProfiles()).filament as unknown as SlicerSetting[];
        })(),
      ]);
      const bambuPresets = bambu.status === 'fulfilled' ? bambu.value : [];
      const orcaPresets = orca.status === 'fulfilled' ? orca.value : [];
      return {
        presets: [...bambuPresets, ...orcaPresets],
        // The clouds are merged into one list; the ids are how the origin badge
        // still tells them apart.
        orcaIds: new Set(orcaPresets.map((p) => p.setting_id)),
      };
    },
    staleTime: FIVE_MINUTES,
  });
  const local = useQuery({
    queryKey: ['product-editor-local-presets'],
    queryFn: api.getLocalPresets,
    staleTime: FIVE_MINUTES,
  });
  const builtin = useQuery({
    queryKey: ['product-editor-builtin-filaments'],
    queryFn: api.getBuiltinFilaments,
    staleTime: FIVE_MINUTES,
  });

  const options = useMemo(
    () =>
      buildFilamentOptions(
        cloud.data?.presets ?? [],
        new Set(),
        local.data?.filament ?? [],
        builtin.data ?? [],
        cloud.data?.orcaIds,
      ),
    [cloud.data, local.data, builtin.data],
  );
  return { options, loading: cloud.isLoading || local.isLoading || builtin.isLoading };
}
