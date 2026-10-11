export function maSeries(closes: (number | null)[], n: number) {
  const out: (number | null)[] = [];
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    const v = closes[i];
    sum += v ?? 0;
    if (i >= n) sum -= closes[i - n] ?? 0;
    out.push(i >= n - 1 && v != null ? +(sum / n).toFixed(3) : null);
  }
  return out;
}
