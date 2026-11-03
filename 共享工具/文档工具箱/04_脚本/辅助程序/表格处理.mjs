import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const FORMULA_ERRORS = "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A";

function requireArgument(value, name) {
  if (!value) {
    throw new Error(`缺少参数：${name}`);
  }
  return path.resolve(value);
}

function safeFileName(value) {
  const cleaned = value.replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_").replace(/[. ]+$/g, "");
  return cleaned || "工作表";
}

async function loadWorkbook(inputPath) {
  try {
    return await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
  } catch (error) {
    throw new Error(`XLSX 表格导入失败，文件可能损坏：${error.message}`);
  }
}

async function inspectWorkbook(workbook, reportPath) {
  const summary = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 8000,
    tableMaxRows: 10,
    tableMaxCols: 12,
    tableMaxCellChars: 120,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: FORMULA_ERRORS,
    options: { useRegex: true, maxResults: 300 },
    maxChars: 8000,
  });
  const errorRecords = (errors.ndjson || "")
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
  const matchedRecords = errorRecords.filter((record) => record.kind === "match").length;
  const countMatch = /Cell search matched (\d+) entries\./.exec(errors.ndjson || "");
  const report = {
    summary: summary.ndjson || "",
    errors: errors.ndjson || "",
    errorCount: matchedRecords || (countMatch ? Number.parseInt(countMatch[1], 10) : 0),
  };
  await fs.mkdir(path.dirname(reportPath), { recursive: true });
  await fs.writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, { encoding: "utf8", flag: "wx" });
  return { type: "xlsx", action: "inspect", report: reportPath };
}

async function renderWorkbook(workbook, outputDir) {
  await fs.mkdir(outputDir, { recursive: true });
  const sheets = workbook.worksheets.items;
  if (!Array.isArray(sheets) || sheets.length === 0) {
    throw new Error("XLSX 表格不包含可渲染的工作表");
  }
  const outputs = [];
  const previews = [];
  for (let index = 0; index < sheets.length; index += 1) {
    const sheet = sheets[index];
    const preview = await workbook.render({
      sheetName: sheet.name,
      range: "A1:P40",
      scale: 1.2,
      format: "png",
    });
    const outputPath = path.join(outputDir, `${String(index + 1).padStart(3, "0")}_${safeFileName(sheet.name)}.png`);
    await fs.writeFile(outputPath, new Uint8Array(await preview.arrayBuffer()), { flag: "wx" });
    outputs.push(outputPath);
    previews.push({ sheetName: sheet.name, path: outputPath });
  }
  return { type: "xlsx", action: "render", outputs, previews };
}

async function main() {
  const [action, inputArg, targetArg] = process.argv.slice(2);
  const inputPath = requireArgument(inputArg, "xlsx");
  const targetPath = requireArgument(targetArg, action === "inspect" ? "json-output" : "output-dir");
  if (action !== "inspect" && action !== "render") {
    throw new Error(`不支持的表格操作：${action || "（空）"}`);
  }
  const workbook = await loadWorkbook(inputPath);
  const result = action === "inspect"
    ? await inspectWorkbook(workbook, targetPath)
    : await renderWorkbook(workbook, targetPath);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

main().catch((error) => {
  process.stderr.write(`表格处理失败：${error.message}\n`);
  process.exitCode = 1;
});
