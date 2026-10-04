import { useId } from "react";

/**
 * The TuningPad mark: three tuning sliders on the V2 blue tile, the top knob in
 * cyan (the policy being tuned). frontend/public/favicon.svg is the same artwork.
 */
export function V2Logo({ size = 28, className }: { size?: number; className?: string }) {
  const gradient = `tp-logo-${useId().replace(/:/g, "")}`;
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className={className} aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#2f7bff" />
          <stop offset="1" stopColor="#1250e6" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill={`url(#${gradient})`} />
      <g stroke="#fff" strokeWidth="2.2" strokeLinecap="round">
        <line x1="7" y1="10" x2="25" y2="10" strokeOpacity=".55" />
        <line x1="7" y1="16" x2="25" y2="16" strokeOpacity=".55" />
        <line x1="7" y1="22" x2="25" y2="22" strokeOpacity=".55" />
      </g>
      <circle cx="20" cy="10" r="3" fill="#5ee7ff" stroke="#fff" strokeWidth="1.5" />
      <circle cx="12" cy="16" r="3" fill="#fff" />
      <circle cx="17" cy="22" r="3" fill="#fff" />
    </svg>
  );
}
