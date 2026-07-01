import type { Visualization } from "../types";
import { compactNumber } from "../utils/formatters";

interface VisualPanelProps {
  visualization: Visualization;
}

export function VisualPanel({ visualization }: VisualPanelProps) {
  const maxValue = Math.max(...visualization.data.map((item) => item.value), 1);

  return (
    <section className="visual-panel">
      <div className="visual-panel__header">
        <div>
          <h3>{visualization.title}</h3>
          {visualization.subtitle ? <p>{visualization.subtitle}</p> : null}
        </div>
        <span>{visualization.unit}</span>
      </div>

      <div className="bar-list">
        {visualization.data.map((item, index) => {
          const width = `${Math.max((item.value / maxValue) * 100, 4)}%`;

          return (
            <div className="bar-row" key={item.label}>
              <span className="bar-row__rank">{index + 1}</span>
              <span className="bar-row__label">{item.label}</span>
              <div className="bar-track" aria-hidden="true">
                <span className="bar-fill" style={{ width }} />
              </div>
              <strong>{compactNumber(item.value)}</strong>
            </div>
          );
        })}
      </div>
    </section>
  );
}
