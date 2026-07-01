import { AlertTriangle, CheckCircle2, CircleHelp } from "lucide-react";

import type { TableStatus } from "../types";

const statusConfig = {
  normal: {
    label: "정상",
    icon: CheckCircle2,
  },
  needs_review: {
    label: "확인 필요",
    icon: CircleHelp,
  },
  suspected_error: {
    label: "오류 의심",
    icon: AlertTriangle,
  },
} satisfies Record<TableStatus, { label: string; icon: typeof CheckCircle2 }>;

interface StatusBadgeProps {
  status: TableStatus;
  label?: string;
}

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const config = statusConfig[status];
  const Icon = config.icon;

  return (
    <span className={`status-badge status-badge--${status}`}>
      <Icon aria-hidden="true" size={14} />
      {label ?? config.label}
    </span>
  );
}
