/** A row of checkboxes for picking a subset of `options`; the result keeps their order. */
export function CheckGroup({ options, value, onChange }: { options: string[]; value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="tp-row">
      {options.map((o) => (
        <label key={o} className="tp-row">
          <input
            type="checkbox"
            checked={value.includes(o)}
            onChange={(e) => onChange(e.target.checked ? options.filter((x) => x === o || value.includes(x)) : value.filter((x) => x !== o))}
          />
          <span className="tp-mono">{o}</span>
        </label>
      ))}
    </div>
  );
}
