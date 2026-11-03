#!/usr/bin/env python3
"""DOCX/PDF 结构检查和文本提取命令行工具。"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader


def inspect_docx(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as package:
            names = set(package.namelist())
    except zipfile.BadZipFile as exc:
        raise ValueError("DOCX 不是有效的 OOXML 压缩包，文件可能已损坏") from exc

    if "word/document.xml" not in names:
        raise ValueError("DOCX 缺少 word/document.xml，文件不是有效的 Word 文档")

    document = Document(path)
    return {
        "type": "docx",
        "paragraphs": len(document.paragraphs),
        "tables": len(document.tables),
        "images": len(document.inline_shapes),
    }


def inspect_pdf(path: Path) -> dict[str, Any]:
    reader = PdfReader(path)
    metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
    return {"type": "pdf", "pages": len(reader.pages), "metadata": metadata}


def extract_docx_text(path: Path) -> str:
    inspect_docx(path)
    document = Document(path)
    blocks: list[str] = []
    blocks.extend(paragraph.text for paragraph in document.paragraphs if paragraph.text)
    for table in document.tables:
        for row in table.rows:
            blocks.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(blocks)


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(path)
    page_texts = [(page.extract_text() or "").rstrip() for page in reader.pages]
    return "\n\n".join(page_texts)


def write_unique_text(output_root: Path, source: Path, text: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = f"{source.stem}_提取文本"
    timestamp: str | None = None
    suffix = 2
    while True:
        if timestamp is None:
            candidate = output_root / f"{base_name}.txt"
        elif suffix == 2:
            candidate = output_root / f"{base_name}_{timestamp}.txt"
        else:
            candidate = output_root / f"{base_name}_{timestamp}_{suffix - 1}.txt"
        try:
            with candidate.open("x", encoding="utf-8") as output_file:
                output_file.write(text)
            return candidate
        except FileExistsError:
            if timestamp is None:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            else:
                suffix += 1


def inspect_document(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return inspect_docx(path)
    if suffix == ".pdf":
        return inspect_pdf(path)
    raise ValueError(f"不支持检查此格式：{suffix or '无扩展名'}")


def extract_document(path: Path, output_root: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        document_type = "docx"
        text = extract_docx_text(path)
    elif suffix == ".pdf":
        document_type = "pdf"
        text = extract_pdf_text(path)
    else:
        raise ValueError(f"不支持提取此格式：{suffix or '无扩展名'}")

    output_path = write_unique_text(output_root, path, text)
    return {
        "type": document_type,
        "action": "extract",
        "output": str(output_path.resolve()),
        "characters": len(text),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 DOCX/PDF 或提取文本")
    parser.add_argument("mode", choices=("inspect", "extract"))
    parser.add_argument("path")
    parser.add_argument("--output-root")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = Path(args.path).resolve()
    try:
        if not path.is_file():
            raise ValueError(f"输入文件不存在：{path}")

        if args.mode == "inspect":
            result = inspect_document(path)
        else:
            if not args.output_root:
                raise ValueError("提取文本必须指定 --output-root")
            result = extract_document(path, Path(args.output_root).resolve())

        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        print(f"文档处理失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
