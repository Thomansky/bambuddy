/** The printer model a slicer preset is for (#3165): "H2S", "H2D". Muted
 *  where it stands for no model in particular, or for one still unset. */
export function ModelBadge({ model, title, muted = false }: { model: string; title?: string; muted?: boolean }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center px-1.5 py-0.5 text-[10px] font-semibold leading-none rounded-full whitespace-nowrap shrink-0 ${
        muted ? 'bg-bambu-dark-tertiary text-bambu-gray' : 'bg-bambu-green/15 text-bambu-green'
      }`}
    >
      {model}
    </span>
  );
}
