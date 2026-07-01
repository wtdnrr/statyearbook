import { BookOpenCheck } from "lucide-react";

export type AppSection = "annual" | "keyStats" | "press";

interface AppHeaderProps {
  activeSection: AppSection;
  onSectionChange: (section: AppSection) => void;
}

const navItems: Array<{ id: AppSection; label: string }> = [
  { id: "annual", label: "통계 연보" },
  { id: "keyStats", label: "주요통계집" },
  { id: "press", label: "보도자료" },
];

export function AppHeader({ activeSection, onSectionChange }: AppHeaderProps) {
  return (
    <header className="app-header">
      <div className="app-logo">
        <BookOpenCheck aria-hidden="true" size={22} />
        <span>통계 연보 검수 보조</span>
      </div>

      <nav className="app-nav" aria-label="주요 메뉴">
        {navItems.map((item) => (
          <button
            className={activeSection === item.id ? "is-active" : ""}
            key={item.id}
            type="button"
            onClick={() => onSectionChange(item.id)}
          >
            {item.label}
          </button>
        ))}
      </nav>
    </header>
  );
}
