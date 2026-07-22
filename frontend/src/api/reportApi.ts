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

export interface ReportImportJob {
  id: number;
  report_id: number | null;
  run_id: number | null;
  source_file_name: string;
  status: "queued" | "running" | "completed" | "failed";
  current_stage: string;
  error_message: string;
}

export async function uploadReport(file: File): Promise<ReportImportJob> {
  const params = new URLSearchParams({ filename: file.name });
  const response = await fetch(`${API_BASE_URL}/api/imports?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? "연보 파일을 업로드하지 못했습니다.");
  }
  return response.json();
}

export async function uploadLegacyOverlay(
  files: File[],
  baseReportId: number,
): Promise<ReportImportJob> {
  const formData = new FormData();
  files.forEach((file) => formData.append("files", file));
  const response = await fetch(
    `${API_BASE_URL}/api/imports/legacy-overlay?base_report_id=${baseReportId}`,
    { method: "POST", body: formData },
  );
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? "2026 테스트 데이터를 업로드하지 못했습니다.");
  }
  return response.json();
}

export async function fetchImportJob(jobId: number): Promise<ReportImportJob> {
  const response = await fetch(`${API_BASE_URL}/api/imports/${jobId}`);
  if (!response.ok) {
    throw new Error("가져오기 작업 상태를 확인하지 못했습니다.");
  }
  return response.json();
}

export async function retryImport(jobId: number): Promise<ReportImportJob> {
  const response = await fetch(`${API_BASE_URL}/api/imports/${jobId}/retry`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error("실패한 연보 처리 작업을 재시도하지 못했습니다.");
  }
  return response.json();
}

export async function waitForImport(
  jobId: number,
  { automaticRetries = 1 }: { automaticRetries?: number } = {},
): Promise<ReportImportJob> {
  let retries = 0;
  for (let attempt = 0; attempt < 400; attempt += 1) {
    const job = await fetchImportJob(jobId);
    if (job.status === "completed") {
      return job;
    }
    if (job.status === "failed") {
      if (retries < automaticRetries) {
        retries += 1;
        await retryImport(jobId);
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        continue;
      }
      throw new Error(job.error_message || "연보 처리 중 오류가 발생했습니다.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1500));
  }
  throw new Error("연보 처리 시간이 초과되었습니다. 작업 상태를 다시 확인해 주세요.");
}
