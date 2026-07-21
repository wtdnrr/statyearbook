import { useRef } from "react";
import { BookOpenCheck, LoaderCircle, Upload } from "lucide-react";

export type AppSection = "annual" | "keyStats" | "press";

interface AppHeaderProps {
  activeSection: AppSection;
  onSectionChange: (section: AppSection) => void;
  onUpload: (file: File) => void;
  uploadState: "idle" | "uploading" | "error";
}

const navItems: Array<{ id: AppSection; label: string }> = [
  { id: "annual", label: "통계 연보" },
  { id: "keyStats", label: "주요통계집" },
  { id: "press", label: "보도자료" },
];

export function AppHeader({ activeSection, onSectionChange, onUpload, uploadState }: AppHeaderProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  return (
    <header className="app-header">
      <div className="app-logo">
        <BookOpenCheck aria-hidden="true" size={22} />
        <span>통계 연보 검수 보조</span>
      </div>

      <div className="app-header__actions">
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
        <input
          ref={fileInputRef}
          className="visually-hidden"
          type="file"
          accept=".xlsx,.hwpx,.md"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) {
              onUpload(file);
            }
            event.target.value = "";
          }}
        />
        <button
          className="report-upload-button"
          type="button"
          disabled={uploadState === "uploading"}
          onClick={() => fileInputRef.current?.click()}
        >
          {uploadState === "uploading" ? (
            <LoaderCircle className="is-spinning" aria-hidden="true" size={18} />
          ) : (
            <Upload aria-hidden="true" size={18} />
          )}
          <span>{uploadState === "uploading" ? "처리 중" : "연보 업로드"}</span>
        </button>
      </div>
    </header>
  );
}
