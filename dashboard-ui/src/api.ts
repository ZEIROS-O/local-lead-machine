export async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: "application/json" } });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `Dashboard request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function params(values: Record<string, string | number | boolean | undefined>): string {
  const result = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== "" && value !== undefined) result.set(key, String(value));
  });
  const query = result.toString();
  return query ? `?${query}` : "";
}
