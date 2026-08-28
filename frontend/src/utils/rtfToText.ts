// Best-effort conversion of an RTF (or RTF-encapsulated HTML) body to plain
// text, for .msg previews whose plain-text body stream is missing. Outlook
// always stores *some* body; this beats showing nothing, and the caller marks
// the result as simplified.
export function rtfToText(rtf: string): string {
  const fromHtml = /\\fromhtml/.test(rtf);
  let text = rtf
    // \*-destinations (metadata like \*\htmltag keeps its text content when
    // encapsulating HTML, so only drop non-nested non-html ones).
    .replace(/\{\\\*\\(?!htmltag)[^{}]*\}/g, '')
    // Font/colour/stylesheet tables carry no body text.
    .replace(/\{\\(?:fonttbl|colortbl|stylesheet)[^{}]*(?:\{[^{}]*\}[^{}]*)*\}/g, '')
    .replace(/\\par[d]?\b/g, '\n')
    .replace(/\\line\b/g, '\n')
    .replace(/\\tab\b/g, '\t')
    .replace(/\\'([0-9a-fA-F]{2})/g, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)))
    .replace(/\\u(-?\d+)\s?\??/g, (_, code: string) => String.fromCharCode((Number(code) + 0x10000) % 0x10000))
    .replace(/\\[a-zA-Z]+-?\d* ?/g, '')
    .replace(/[{}]/g, '')
    .replace(/\r/g, '');
  if (fromHtml) {
    text = text
      .replace(/<style[\s\S]*?<\/style>/gi, '')
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/<[^>]+>/g, '')
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&')
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"');
  }
  return text.replace(/\n{3,}/g, '\n\n').trim();
}
