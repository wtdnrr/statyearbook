import { SlidersHorizontal } from "lucide-react";

interface ValidationTypeFilterProps {
  types: string[];
  hiddenTypes: ReadonlySet<string>;
  align?: "left" | "right";
  onVisibilityChange: (type: string, visible: boolean) => void;
  onShowAll: () => void;
  onHideAll: () => void;
}

export function ValidationTypeFilter({
  types,
  hiddenTypes,
  align = "left",
  onVisibilityChange,
  onShowAll,
  onHideAll,
}: ValidationTypeFilterProps) {
  const visibleCount = types.filter((type) => !hiddenTypes.has(type)).length;
  const allVisible = visibleCount === types.length;

  return (
    <details className={`validation-type-filter validation-type-filter--${align}`}>
      <summary>
        <SlidersHorizontal aria-hidden="true" size={15} />
        <span>검수 항목</span>
        <strong>
          {visibleCount}/{types.length}
        </strong>
      </summary>
      <div className="validation-type-filter__menu">
        <div className="validation-type-filter__heading">
          <strong>표시할 검수</strong>
          <button type="button" onClick={allVisible ? onHideAll : onShowAll}>
            {allVisible ? "모두 숨기기" : "모두 표시"}
          </button>
        </div>
        <div className="validation-type-filter__options">
          {types.map((type) => (
            <label key={type}>
              <input
                checked={!hiddenTypes.has(type)}
                onChange={(event) => onVisibilityChange(type, event.target.checked)}
                type="checkbox"
              />
              <span>{type}</span>
            </label>
          ))}
        </div>
      </div>
    </details>
  );
}
