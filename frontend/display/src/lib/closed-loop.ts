function formatCny(value: string | number, name: string): string {
  const amount = Number(value);
  if (!Number.isFinite(amount)) throw new Error(`${name}不是有效数字`);
  return `¥${amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatMoney(value: string | number): string {
  return formatCny(value, "金额");
}

export function formatPrice(value: string | number): string {
  return formatCny(value, "价格");
}

export function formatPercent(value: number): string {
  if (!Number.isFinite(value)) throw new Error("比例不是有效数字");
  return `${(value * 100).toFixed(1)}%`;
}

export function sideLabel(side: "buy" | "sell"): string {
  return side === "buy" ? "买入" : "卖出";
}
