import type { ReportPayload } from "../types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";

export async function fetchReport(reportId?: string): Promise<ReportPayload> {
  const params = new URLSearchParams();
  if (reportId) {
    params.set("report_id", reportId);
  }
  const query = params.toString();
  const response = await fetch(`${API_BASE_URL}/api/report${query ? `?${query}` : ""}`);

  if (!response.ok) {
    throw new Error("통계연보 데이터를 불러오지 못했습니다.");
  }

  return response.json();
}
