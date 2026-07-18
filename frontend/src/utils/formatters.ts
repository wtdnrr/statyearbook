export function formatCellValue(value: string | number, columnLabel = "") {
  if (typeof value === "number") {
    if (/연도|year/i.test(columnLabel) && Number.isInteger(value)) {
      return String(value);
    }

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

const issueTypeOrder = new Map(
  [
    "합계 검수",
    "비율 검수",
    "오탈자 검수",
    "번역 검수",
    "메타정보 검수",
    "이상치 검수",
    "파란색 표기 확인",
  ].map((type, index) => [type, index]),
);

export function issueCountLabel(issueCounts: Record<string, number>) {
  const entries = Object.entries(issueCounts).sort(
    ([left], [right]) => (issueTypeOrder.get(left) ?? 999) - (issueTypeOrder.get(right) ?? 999),
  );

  if (entries.length === 0) {
    return "검수 특이사항 없음";
  }

  return entries.map(([label, count]) => `${label} ${count}건`).join(" | ");
}
