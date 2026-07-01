export function formatCellValue(value: string | number) {
  if (typeof value === "number") {
    return new Intl.NumberFormat("ko-KR", {
      maximumFractionDigits: Number.isInteger(value) ? 0 : 1,
    }).format(value);
  }

  return value;
}

export function compactNumber(value: number) {
  return new Intl.NumberFormat("ko-KR", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

export function issueCountLabel(issueCounts: Record<string, number>) {
  const entries = Object.entries(issueCounts);

  if (entries.length === 0) {
    return "검수 특이사항 없음";
  }

  return entries.map(([label, count]) => `${label} ${count}건`).join(" | ");
}
