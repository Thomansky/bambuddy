/**
 * Empty stand-in for Node's `stream` in the browser build.
 *
 * iconv-lite feature-detects `require('stream').Transform` to decide whether
 * to publish its streaming API, which nothing in the browser uses. That read
 * sits outside the try/catch around the require, so it lands on whatever Vite
 * substituted for the built-in — and Vite picks that substitute from
 * `isProduction` (`process.env.NODE_ENV === 'production'`), NOT from `--mode`:
 * a production build gets `module.exports = {}`, any other build a Proxy that
 * throws on property access. Every `vite build` defaults NODE_ENV to
 * production, so the shipped bundle and `vite build --mode development` alike
 * get the empty object and survive the read (the streaming API simply stays
 * off). A build made with NODE_ENV set to anything else does not, and that is
 * src/__tests__/utils/msgFileBundle.test.ts: vitest runs with NODE_ENV=test,
 * where the substitute raises Vite's "has been externalized for browser
 * compatibility" error out of iconv-lite's module init.
 *
 * An empty module answers the detection plainly in every environment: there is
 * no stream module here, so the streaming API stays off. Note that the alias
 * in vite.config.ts is global — a dependency that genuinely needs `stream`
 * would get this `{}` rather than a Vite diagnostic naming the module.
 */
export default {};
