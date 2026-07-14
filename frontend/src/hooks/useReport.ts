import { useEffect, useState } from "react";

import { fetchReport } from "../api/reportApi";
import type { ReportPayload } from "../types";

type LoadState =
  | { status: "loading"; data: null; error: null }
  | { status: "success"; data: ReportPayload; error: null }
  | { status: "error"; data: null; error: string };

export function useReport(reportId?: string) {
  const [state, setState] = useState<LoadState>({
    status: "loading",
    data: null,
    error: null,
  });

  useEffect(() => {
    let isMounted = true;
    setState({ status: "loading", data: null, error: null });

    fetchReport(reportId)
      .then((data) => {
        if (isMounted) {
          setState({ status: "success", data, error: null });
        }
      })
      .catch((error: Error) => {
        if (isMounted) {
          setState({ status: "error", data: null, error: error.message });
        }
      });

    return () => {
      isMounted = false;
    };
  }, [reportId]);

  return state;
}
