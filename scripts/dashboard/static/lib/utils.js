/* ka-sfskills · shared dashboard utilities */

/** @param {string} s */
export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Pad a number to width 2 with a leading zero. */
export const pad = (n) => String(n).padStart(2, "0");

/** Human-friendly relative time ("just now", "5m ago", "3d ago", or a date).
 * @param {Date} date
 */
export function relativeTime(date) {
  const t = date.getTime();
  if (!t) return "—";
  const seconds = Math.floor((Date.now() - t) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return date.toLocaleDateString();
}
