/**
 * Empty stand-in for Node's `stream` in the browser build.
 *
 * iconv-lite feature-detects `require('stream').Transform` to decide whether
 * to publish its streaming API, which nothing in the browser uses. That read
 * sits outside the try/catch around the require, so Vite's own stand-in for a
 * missing built-in — which throws on every property access outside production
 * builds — takes iconv-lite's module init down with it: `vite build --mode
 * development`, the build the next person will reach for to debug this chunk,
 * fails, and the dev server logs the externalized-module warning on each
 * preview. An empty module answers the detection plainly: there is no stream
 * module here, so the streaming API stays off.
 */
export default {};
