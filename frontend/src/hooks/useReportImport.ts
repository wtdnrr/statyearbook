import { useCallback, useState } from "react";

import { uploadReport, waitForImport, type ReportImportJob } from "../api/reportApi";


export type UploadState = "idle" | "uploading" | "error";

export function useReportImport(onCompleted: (job: ReportImportJob) => void) {
  const [uploadState, setUploadState] = useState<UploadState>("idle");

  const upload = useCallback(
    async (file: File) => {
      setUploadState("uploading");
      try {
        const queued = await uploadReport(file);
        const completed = await waitForImport(queued.id);
        onCompleted(completed);
        setUploadState("idle");
      } catch (error) {
        setUploadState("error");
        window.alert(error instanceof Error ? error.message : "연보 처리 중 오류가 발생했습니다.");
      }
    },
    [onCompleted],
  );

  return { upload, uploadState };
}
