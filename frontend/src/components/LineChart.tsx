const W = 520;
const H = 190;
const L = 44;
const R = 10;
const T = 10;
const B = 22;
const COLORS = ["var(--v2-primary)", "#14c9c9", "#ff7d00", "#722ed1", "#00b42a"];

function fmt(v: number): string {
  const a = Math.abs(v);
  if (a >= 1000) return `${(v / 1000).toFixed(1)}k`;
  if (a >= 10) return v.toFixed(0);
  if (a >= 1) return v.toFixed(2);
  return v.toPrecision(2);
}

/** Small multi-series line chart over training steps (inline SVG, V2 palette). */
export function LineChart({ series, empty, fixed01 }: {
  series: { name: string; points: [number, number][] }[];
  empty: string;
  fixed01?: boolean;
}) {
  const pts = series.flatMap((s) => s.points);
  if (pts.length === 0) return <p className="v2-muted">{empty}</p>;
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs, x0 + 1);
  let y0 = fixed01 ? 0 : Math.min(...ys);
  let y1 = fixed01 ? 1 : Math.max(...ys);
  if (y0 === y1) {
    y0 -= 0.5;
    y1 += 0.5;
  }
  const sx = (x: number) => L + ((x - x0) / (x1 - x0)) * (W - L - R);
  const sy = (y: number) => H - B - ((y - y0) / (y1 - y0)) * (H - T - B);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => y0 + f * (y1 - y0));
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="tp-chart" role="img">
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={sy(v)} y2={sy(v)} stroke="var(--v2-line)" strokeDasharray="3 4" />
            <text x={L - 6} y={sy(v) + 4} textAnchor="end">{fmt(v)}</text>
          </g>
        ))}
        <text x={L} y={H - 4}>{x0}</text>
        <text x={W - R} y={H - 4} textAnchor="end">{x1}</text>
        {series.map((s, i) => (
          <g key={s.name}>
            <polyline fill="none" stroke={COLORS[i % COLORS.length]} strokeWidth="2"
              points={s.points.map(([x, y]) => `${sx(x).toFixed(1)},${sy(y).toFixed(1)}`).join(" ")} />
            {s.points.length < 40 && s.points.map(([x, y]) => (
              <circle key={x} cx={sx(x)} cy={sy(y)} r="2.5" fill={COLORS[i % COLORS.length]} />
            ))}
          </g>
        ))}
      </svg>
      {series.length > 1 && (
        <div className="tp-legend">
          {series.map((s, i) => (
            <span key={s.name}><i style={{ background: COLORS[i % COLORS.length] }} />{s.name}</span>
          ))}
        </div>
      )}
    </div>
  );
}
