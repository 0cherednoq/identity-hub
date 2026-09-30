export const MAX_IMPORT_BYTES = 10 * 1024 * 1024;
export const MAX_IMPORT_RECORDS = 10_000;

export type ImportFormat = "csv" | "jsonl";

export interface PreparedImportFile {
  bytes: number;
  content: string;
  format: ImportFormat;
  name: string;
  records: number;
}

export function importFormat(name: string): ImportFormat | null {
  const lower = name.toLowerCase();
  if (lower.endsWith(".csv")) return "csv";
  if (lower.endsWith(".jsonl") || lower.endsWith(".ndjson")) return "jsonl";
  return null;
}

export function estimateImportRecords(
  content: string,
  format: ImportFormat,
): number {
  const lines = content.split(/\r?\n/u).filter((line) => line.trim() !== "");
  return Math.max(0, format === "csv" ? lines.length - 1 : lines.length);
}

export async function prepareImportFile(
  file: File,
): Promise<PreparedImportFile> {
  const format = importFormat(file.name);
  if (format === null)
    throw new Error("Выберите файл .csv, .jsonl или .ndjson");
  if (file.size > MAX_IMPORT_BYTES)
    throw new Error("Файл превышает лимит 10 МиБ");
  const content = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      if (typeof reader.result === "string") resolve(reader.result);
      else reject(new Error("Не удалось прочитать файл"));
    });
    reader.addEventListener("error", () => {
      reject(new Error("Не удалось прочитать файл"));
    });
    reader.readAsText(file, "utf-8");
  });
  const records = estimateImportRecords(content, format);
  if (records === 0) throw new Error("Файл не содержит записей");
  if (records > MAX_IMPORT_RECORDS)
    throw new Error("В файле больше 10 000 записей");
  return { bytes: file.size, content, format, name: file.name, records };
}

export function parseBatchLabels(value: string): Record<string, string> {
  if (value.trim() === "") return {};
  const labels: Record<string, string> = {};
  for (const raw of value.split(",")) {
    const [key, ...rest] = raw.split("=");
    const normalizedKey = key?.trim() ?? "";
    const normalizedValue = rest.join("=").trim();
    if (normalizedKey === "" || normalizedValue === "") {
      throw new Error("Метки указываются как key=value через запятую");
    }
    labels[normalizedKey] = normalizedValue;
  }
  if (Object.keys(labels).length > 100)
    throw new Error("Допустимо не более 100 меток");
  return labels;
}
