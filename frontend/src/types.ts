export type TableStatus = "normal" | "needs_review" | "suspected_error";
export type Severity = "info" | "warning" | "critical";
export type Tone = "neutral" | "blue" | "green" | "red" | "amber";

export interface Metric {
  label: string;
  value: string;
  caption?: string;
  tone: Tone;
}

export interface ColumnDefinition {
  key: string;
  label: string;
  label_en?: string;
  align: "left" | "right" | "center";
  width?: string;
}

export interface ValidationIssue {
  id: string;
  type: string;
  location: string;
  row_index?: number;
  col_index?: number;
  current_value: string;
  expected_value?: string;
  difference?: string;
  status: string;
  severity: Severity;
  detail: string;
  formula?: string;
}

export interface ChangeItem {
  id: string;
  category: string;
  item: string;
  previous: string;
  current: string;
  status: string;
}

export interface VisualizationSeries {
  label: string;
  value: number;
  previous?: number;
}

export interface Visualization {
  id: string;
  title: string;
  subtitle?: string;
  kind: "bar" | "line" | "rank";
  unit: string;
  data: VisualizationSeries[];
}

export interface TableMetadata {
  original_file: string;
  sheet_name: string;
  cell_range: string;
  note: string;
  source: string;
  base_date: string;
  extracted_at: string;
}

export interface StatTable {
  id: string;
  code: string;
  title: string;
  title_en: string;
  section_title: string;
  section_title_en: string;
  domain: string;
  unit: string;
  sheet_name: string;
  status: TableStatus;
  status_label: string;
  year_range: string;
  updated_at: string;
  theme: "blue" | "red" | "green";
  columns: ColumnDefinition[];
  rows: Array<Record<string, string | number>>;
  summary: string[];
  key_figures: Metric[];
  checks: ValidationIssue[];
  changes: ChangeItem[];
  visualizations: Visualization[];
  metadata: TableMetadata;
}

export interface ReportSummary {
  file_name: string;
  base_year: string;
  total_tables: number;
  normal_count: number;
  needs_review_count: number;
  suspected_error_count: number;
  issue_counts: Record<string, number>;
}

export interface PressInsight {
  id: string;
  title: string;
  body: string;
  table_id: string;
  tone: "neutral" | "increase" | "risk" | "notable";
}

export interface ReportPayload {
  summary: ReportSummary;
  tables: StatTable[];
  press_insights: PressInsight[];
}
