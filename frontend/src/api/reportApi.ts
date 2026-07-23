import type { ReportPayload, StatTable } from "../types";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const API_BASE_URL = normalizeApiBaseUrl(
  import.meta.env.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL,
);

export interface ReportImportJob {
  id: number;
  report_id: number | null;
  run_id: number | null;
  source_file_name: string;
  status: "queued" | "running" | "completed" | "failed";
  current_stage: string;
  error_message: string;
}

export async function fetchReport(reportId?: string): Promise<ReportPayload> {
  return requestJson<ReportPayload>(
    apiPath("/api/report", { report_id: reportId }),
    undefined,
    "통계연보 데이터를 불러오지 못했습니다.",
  );
}

export async function fetchTable(tableId: string, reportId?: string): Promise<StatTable> {
  return requestJson<StatTable>(
    apiPath(`/api/tables/${encodeURIComponent(tableId)}`, { report_id: reportId }),
    undefined,
    "통계표 상세 데이터를 불러오지 못했습니다.",
  );
}

export async function uploadReport(file: File): Promise<ReportImportJob> {
  return requestJson<ReportImportJob>(
    apiPath("/api/imports", { filename: file.name }),
    {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    },
    "연보 파일을 업로드하지 못했습니다.",
  );
}

export async function fetchImportJob(jobId: number): Promise<ReportImportJob> {
  return requestJson<ReportImportJob>(
    `/api/imports/${jobId}`,
    undefined,
    "가져오기 작업 상태를 확인하지 못했습니다.",
  );
}

export async function retryImport(jobId: number): Promise<ReportImportJob> {
  return requestJson<ReportImportJob>(
    `/api/imports/${jobId}/retry`,
    { method: "POST" },
    "실패한 연보 처리 작업을 재시도하지 못했습니다.",
  );
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
        await delay(1000);
        continue;
      }
      throw new Error(job.error_message || "연보 처리 중 오류가 발생했습니다.");
    }
    await delay(1500);
  }
  throw new Error("연보 처리 시간이 초과되었습니다. 작업 상태를 다시 확인해 주세요.");
}

function apiPath(path: string, values: Record<string, string | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value) {
      params.set(key, value);
    }
  }
  const query = params.toString();
  return query ? `${path}?${query}` : path;
}

async function requestJson<T>(
  path: string,
  init: RequestInit | undefined,
  fallbackMessage: string,
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, init);
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: string } | null;
    throw new Error(payload?.detail ?? fallbackMessage);
  }
  return response.json() as Promise<T>;
}

function normalizeApiBaseUrl(value: string): string {
  const normalized = value
    .trim()
    .replace(/^VITE_API_BASE_URL\s*=\s*/, "")
    .replace(/\/+$/, "");
  return normalized || DEFAULT_API_BASE_URL;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}
