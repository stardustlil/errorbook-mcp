from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from latex2mathml.converter import convert as latex_to_mathml
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin
from pypdf import PdfReader

from .db import json_dumps
from .errors import ExportError, NotFoundError, ValidationError
from .scheduler import iso_utc, parse_datetime, utc_now
from .schemas import ReviewSheetRequest
from .service import ErrorbookService, _request_hash

MATHML_NAMESPACE = "http://www.w3.org/1998/Math/MathML"
DANGEROUS_LATEX = re.compile(
    r"\\(?:href|style|class|cssId|url|includegraphics|input|include|write|"
    r"newcommand|renewcommand|providecommand|newenvironment|def|let|csname|html)\b",
    re.IGNORECASE,
)
FORBIDDEN_MATHML_ATTRIBUTES = {
    "href",
    "src",
    "style",
    "class",
    "id",
    "actiontype",
}
ALLOWED_MATHML_ATTRIBUTES = {
    "accent",
    "accentunder",
    "align",
    "bevelled",
    "close",
    "columnalign",
    "columnlines",
    "columnspacing",
    "columnspan",
    "depth",
    "display",
    "displaystyle",
    "encoding",
    "fence",
    "form",
    "height",
    "linethickness",
    "lspace",
    "mathsize",
    "mathvariant",
    "maxsize",
    "minsize",
    "movablelimits",
    "notation",
    "open",
    "rowalign",
    "rowlines",
    "rowspacing",
    "rowspan",
    "rspace",
    "scriptlevel",
    "separator",
    "separators",
    "stretchy",
    "subscriptshift",
    "superscriptshift",
    "symmetric",
    "voffset",
    "width",
}
MAX_FORMULA_LENGTH = 10_000
MAX_FORMULAS_PER_FIELD = 200
EXPORT_LEASE_DURATION = timedelta(minutes=5)


def _normalize_math_delimiters(text: str) -> str:
    """Normalize TeX delimiters commonly emitted by OCR into dollar math."""
    text = re.sub(
        r"\\\[(.*?)\\\]", lambda match: f"$$\n{match.group(1)}\n$$", text, flags=re.DOTALL
    )
    return re.sub(r"\\\((.*?)\\\)", lambda match: f"${match.group(1)}$", text, flags=re.DOTALL)


def _validate_formula_markup(text: str, *, context: str) -> None:
    """Reject common TeX that was left in prose instead of marked as math."""
    prose = re.sub(r"\$\$.*?\$\$|\$[^$\n]+\$", "", text, flags=re.DOTALL)
    if re.search(
        r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|iint|oint|lim|cdot|times|leq?|geq?|neq|pm|mid|begin|end)\b",
        prose,
    ):
        raise ExportError(
            f"LaTeX command must be enclosed in math delimiters in {context}",
            details={"context": context},
        )
    if re.search(r"(?<![\w$])[A-Za-z](?:[A-Za-z0-9]*)\s*[\^_]\s*\{?[-+A-Za-z0-9]", prose):
        raise ExportError(
            f"Superscripts and subscripts must be enclosed in math delimiters in {context}",
            details={"context": context},
        )


CSS = """
@page {
  size: A4;
  margin: 15mm 14mm 18mm;
  @bottom-center {
    content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
    font-size: 8.5pt;
    color: #666;
  }
}
* { box-sizing: border-box; }
html { color: #171717; background: #fff; }
body {
  margin: 0;
  font-family: __PDF_FONT__;
  font-size: 11pt;
  line-height: 1.65;
  letter-spacing: 0;
}
h1 { margin: 0 0 2mm; font-size: 20pt; font-weight: 700; letter-spacing: 0; }
.sheet-meta {
  display: flex;
  justify-content: space-between;
  gap: 8mm;
  padding-bottom: 4mm;
  margin-bottom: 7mm;
  border-bottom: 0.45mm solid #222;
  color: #555;
  font-size: 9pt;
}
.identity { margin: 0 0 8mm; color: #333; }
.problem {
  margin: 0 0 10mm;
  break-inside: avoid;
  page-break-inside: avoid;
}
.problem-heading {
  display: flex;
  align-items: baseline;
  gap: 2.5mm;
  margin-bottom: 2mm;
  break-after: avoid;
}
.position { font-size: 12pt; font-weight: 700; }
.problem-number {
  color: #555;
  font-family: Consolas, "Courier New", monospace;
  font-size: 8.5pt;
}
.subject { margin-left: auto; color: #666; font-size: 8.5pt; }
.markdown > :first-child { margin-top: 0; }
.markdown > :last-child { margin-bottom: 0; }
p { margin: 0 0 2.5mm; }
ul, ol { margin: 1.5mm 0 2.5mm 6mm; padding-left: 5mm; }
pre, code { font-family: Consolas, monospace; }
code { background: #f3f3f3; padding: 0.2mm 0.8mm; }
pre { white-space: pre-wrap; padding: 2.5mm; border: 0.2mm solid #ddd; }
.choices { margin-top: 3mm; }
.choice {
  display: grid;
  grid-template-columns: 8mm 1fr;
  gap: 1.5mm;
  padding: 1.2mm 0;
  break-inside: avoid;
}
.choice-label { font-weight: 700; }
.work-area {
  height: 48mm;
  margin-top: 4mm;
  background: repeating-linear-gradient(
    to bottom,
    transparent 0,
    transparent 9mm,
    #e8e8e8 9.2mm,
    transparent 9.4mm
  );
}
.work-area.compact { height: 16mm; }
.math.inline { white-space: nowrap; }
.math.block { overflow: hidden; margin: 3mm 0; text-align: center; }
math[display="block"] { margin: 0 auto; max-width: 100%; }
.empty { color: #777; font-style: italic; }
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SafeMarkdownRenderer:
    def __init__(self) -> None:
        self.context = "unknown field"
        self.formula_count = 0

    def render(self, text: str | None, *, context: str) -> str:
        if not text:
            return ""
        self.context = context
        self.formula_count = 0
        text = _normalize_math_delimiters(text)
        _validate_formula_markup(text, context=context)
        markdown = MarkdownIt(
            "commonmark",
            {"html": False, "linkify": False, "typographer": False},
        )
        markdown.disable(["link", "image", "autolink"])
        markdown.use(
            dollarmath_plugin,
            allow_labels=False,
            renderer=self._render_math,
        )
        return markdown.render(text)

    def _render_math(self, source: str, options: dict[str, Any]) -> str:
        self.formula_count += 1
        if self.formula_count > MAX_FORMULAS_PER_FIELD:
            raise ExportError(
                f"Too many formulas in {self.context}",
                details={"context": self.context, "limit": MAX_FORMULAS_PER_FIELD},
            )
        if len(source) > MAX_FORMULA_LENGTH:
            raise ExportError(
                f"Formula is too long in {self.context}",
                details={"context": self.context, "limit": MAX_FORMULA_LENGTH},
            )
        if DANGEROUS_LATEX.search(source):
            raise ExportError(
                f"Unsafe LaTeX command in {self.context}",
                details={"context": self.context},
            )

        normalized = source.replace(r"\begin{aligned}", r"\begin{align*}").replace(
            r"\end{aligned}", r"\end{align*}"
        )
        try:
            mathml = latex_to_mathml(
                normalized,
                display="block" if options.get("display_mode") else "inline",
            )
            root = ET.fromstring(mathml)
        except Exception as exc:
            raise ExportError(
                f"Invalid LaTeX formula in {self.context}",
                details={"context": self.context, "formula": source[:200]},
            ) from exc
        self._validate_mathml(root, source)
        return mathml

    def _validate_mathml(self, root: ET.Element, source: str) -> None:
        for element in root.iter():
            if not element.tag.startswith(f"{{{MATHML_NAMESPACE}}}"):
                raise ExportError(
                    f"Unexpected XML namespace in {self.context}",
                    details={"context": self.context},
                )
            for attribute in element.attrib:
                local_name = attribute.rsplit("}", 1)[-1].lower()
                if (
                    local_name in FORBIDDEN_MATHML_ATTRIBUTES
                    or local_name.startswith("on")
                    or local_name not in ALLOWED_MATHML_ATTRIBUTES
                ):
                    raise ExportError(
                        f"Unsafe MathML attribute in {self.context}",
                        details={"context": self.context, "attribute": local_name},
                    )
            for value in (element.text, element.tail):
                if value and "\\" in value:
                    raise ExportError(
                        f"Unknown LaTeX command in {self.context}",
                        details={"context": self.context, "formula": source[:200]},
                    )


class PdfExporter:
    def __init__(self, service: ErrorbookService) -> None:
        self.service = service
        self.settings = service.settings

    def create_review_sheet(
        self,
        request: ReviewSheetRequest,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self.service._validate_idempotency_key(idempotency_key)
        request_hash = _request_hash(request.model_dump(mode="json"))
        scope = "create_review_sheet"
        now = utc_now()

        with self.service.db.transaction() as connection:
            preliminary_replay = self.service._idempotency_replay(
                connection, scope, idempotency_key, request_hash
            )
        preselected: list[dict[str, Any]] | None = None
        preselection_meta: dict[str, Any] | None = None
        if preliminary_replay is None:
            preselected, preselection_meta = self.service.select_review_candidates(
                mode=request.mode,
                numbers=request.numbers,
                subjects=request.subjects,
                tags=request.tags,
                horizon_days=request.horizon_days,
                max_questions=request.max_questions,
                now=now,
            )
            if request.mode == "numbers":
                found = {item["number"] for item in preselected}
                missing = sorted({number.strip().upper() for number in request.numbers} - found)
                if missing:
                    raise NotFoundError(
                        "Some requested problems were not found or are not active",
                        details={"missing_numbers": missing},
                    )
            if not preselected:
                raise ValidationError(
                    "No problems match this review sheet selection",
                    details={"mode": request.mode, "horizon_days": request.horizon_days},
                )

        with self.service.db.transaction(immediate=True) as connection:
            replay = self.service._idempotency_replay(
                connection, scope, idempotency_key, request_hash
            )
            if replay is not None:
                if replay.get("status") != "generating":
                    return replay
                export_id = replay["export_id"]
                review_set_id = replay["review_set_id"]
                export_state = connection.execute(
                    """
                    SELECT status, lease_expires_at FROM exports WHERE id = ?
                    """,
                    (export_id,),
                ).fetchone()
                if export_state is None:
                    raise NotFoundError(f"Export {export_id} was not found")
                if export_state["status"] != "generating":
                    return self.get_export(export_id)
                if (
                    export_state["lease_expires_at"]
                    and parse_datetime(export_state["lease_expires_at"]) > now
                ):
                    replay["lease_expires_at"] = export_state["lease_expires_at"]
                    return replay
                lease_token = str(uuid.uuid4())
                lease_expires_at = iso_utc(now + EXPORT_LEASE_DURATION)
                connection.execute(
                    """
                    UPDATE exports SET lease_token = ?, lease_expires_at = ? WHERE id = ?
                    """,
                    (lease_token, lease_expires_at, export_id),
                )
                replay["lease_expires_at"] = lease_expires_at
                connection.execute(
                    """
                    UPDATE idempotency_records SET response_json = ?
                    WHERE scope = ? AND key = ?
                    """,
                    (json_dumps(replay), scope, idempotency_key),
                )
                selected_numbers = replay["selected_numbers"]
                selection_meta = replay["selection"]
                stored_items = connection.execute(
                    """
                    SELECT problem_id, snapshot_json FROM review_set_items
                    WHERE review_set_id = ? ORDER BY position
                    """,
                    (review_set_id,),
                ).fetchall()
                snapshots = [json.loads(item["snapshot_json"]) for item in stored_items]
            else:
                assert preselected is not None and preselection_meta is not None
                selected = preselected
                selection_meta = preselection_meta
                placeholders = ",".join("?" for _ in selected)
                active_ids = {
                    row["id"]
                    for row in connection.execute(
                        f"SELECT id FROM problems WHERE status = 'active' AND id IN ({placeholders})",
                        tuple(item["id"] for item in selected),
                    ).fetchall()
                }
                if len(active_ids) != len(selected):
                    raise ValidationError("Selected problems changed state; retry the export")

                review_set_id = str(uuid.uuid4())
                export_id = str(uuid.uuid4())
                lease_token = str(uuid.uuid4())
                lease_expires_at = iso_utc(now + EXPORT_LEASE_DURATION)
                selected_numbers = [item["number"] for item in selected]
                selection_payload = {
                    "request": request.model_dump(mode="json"),
                    "result": selection_meta,
                    "as_of": iso_utc(now),
                }
                connection.execute(
                    """
                    INSERT INTO review_sets(id, title, selection_json, algorithm_version, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        review_set_id,
                        request.title,
                        json_dumps(selection_payload),
                        self.service.algorithm_version,
                        iso_utc(now),
                    ),
                )
                snapshots = []
                for position, problem in enumerate(selected, start=1):
                    snapshot = {
                        key: problem[key]
                        for key in (
                            "number",
                            "kind",
                            "subject",
                            "stem_markdown",
                            "choices",
                            "source",
                            "tags",
                            "version",
                        )
                    }
                    snapshot["position"] = position
                    snapshot["priority"] = problem["priority"]
                    snapshots.append(snapshot)
                    connection.execute(
                        """
                        INSERT INTO review_set_items(
                            review_set_id, position, problem_id, problem_number,
                            problem_version, priority_score, snapshot_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            review_set_id,
                            position,
                            problem["id"],
                            problem["number"],
                            problem["version"],
                            problem["priority"]["score"],
                            json_dumps(snapshot),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO exports(
                        id, review_set_id, status, lease_token, lease_expires_at, created_at
                    ) VALUES (?, ?, 'generating', ?, ?, ?)
                    """,
                    (
                        export_id,
                        review_set_id,
                        lease_token,
                        lease_expires_at,
                        iso_utc(now),
                    ),
                )
                initial_response = {
                    "ok": True,
                    "status": "generating",
                    "export_id": export_id,
                    "review_set_id": review_set_id,
                    "selected_numbers": selected_numbers,
                    "selection": selection_meta,
                    "lease_expires_at": lease_expires_at,
                }
                self.service._save_idempotency(
                    connection,
                    scope,
                    idempotency_key,
                    request_hash,
                    initial_response,
                    now,
                )

        try:
            self._cleanup_abandoned_render_files(export_id)
            result = self._render_export(
                export_id=export_id,
                lease_token=lease_token,
                title=request.title,
                snapshots=snapshots,
                generated_at=now,
            )
        except Exception as exc:
            failure = exc if isinstance(exc, ExportError) else ExportError(str(exc))
            failed_response = {
                "ok": False,
                "status": "failed",
                "export_id": export_id,
                "review_set_id": review_set_id,
                "selected_numbers": selected_numbers,
                "error": failure.as_dict()["error"],
            }
            with self.service.db.transaction(immediate=True) as connection:
                updated = connection.execute(
                    """
                    UPDATE exports SET status = 'failed', error_message = ?, completed_at = ?,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE id = ? AND lease_token = ?
                    """,
                    (
                        failure.message,
                        iso_utc(utc_now()),
                        export_id,
                        lease_token,
                    ),
                )
                if updated.rowcount:
                    connection.execute(
                        """
                        UPDATE idempotency_records SET response_json = ?
                        WHERE scope = ? AND key = ?
                        """,
                        (json_dumps(failed_response), scope, idempotency_key),
                    )
                else:
                    return self.get_export(export_id)
            return failed_response

        ready_response = {
            "ok": True,
            "status": "ready",
            "export_id": export_id,
            "review_set_id": review_set_id,
            "selected_numbers": selected_numbers,
            "selection": selection_meta,
            **result,
        }
        try:
            with self.service.db.transaction(immediate=True) as connection:
                updated = connection.execute(
                    """
                    UPDATE exports SET status = 'ready', questions_path = ?, questions_sha256 = ?,
                        completed_at = ?,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE id = ? AND lease_token = ?
                    """,
                    (
                        result["questions"]["relative_path"],
                        result["questions"]["sha256"],
                        iso_utc(utc_now()),
                        export_id,
                        lease_token,
                    ),
                )
                if not updated.rowcount:
                    self._discard_render_result(result)
                    return self.get_export(export_id)
                connection.execute(
                    """
                    UPDATE idempotency_records SET response_json = ?
                    WHERE scope = ? AND key = ?
                    """,
                    (json_dumps(ready_response), scope, idempotency_key),
                )
        except BaseException:
            self._discard_render_result(result)
            raise
        return ready_response

    def get_export(self, export_id: str) -> dict[str, Any]:
        row = self.service.db.fetch_one("SELECT * FROM exports WHERE id = ?", (export_id,))
        if row is None:
            raise NotFoundError(f"Export {export_id} was not found")
        result: dict[str, Any] = {
            "ok": True,
            "export_id": export_id,
            "review_set_id": row["review_set_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
        if row["status"] == "generating":
            result["lease_expires_at"] = row["lease_expires_at"]
        if row["status"] == "ready":
            result["questions"] = self._file_descriptor(
                export_id, "questions", row["questions_path"], row["questions_sha256"]
            )
        elif row["status"] == "failed":
            result["error_message"] = row["error_message"]
        return result

    def list_exports(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100")
        if offset < 0:
            raise ValidationError("offset must be non-negative")
        rows = self.service.db.fetch_all(
            """
            SELECT e.id, e.status, e.questions_path, e.questions_sha256,
                   e.created_at, e.completed_at, r.title
            FROM exports e JOIN review_sets r ON r.id = e.review_set_id
            ORDER BY e.created_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        items = []
        for row in rows:
            path = None
            exists = False
            if row["questions_path"]:
                path = self._safe_export_path(row["questions_path"], require_exists=False)
                exists = path.is_file()
            items.append(
                {
                    "export_id": row["id"],
                    "title": row["title"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "completed_at": row["completed_at"],
                    "questions": {
                        "relative_path": row["questions_path"],
                        "sha256": row["questions_sha256"],
                        "exists": exists,
                        "size_bytes": path.stat().st_size if exists else 0,
                    },
                }
            )
        return {"ok": True, "items": items, "limit": limit, "offset": offset}

    def delete_export(self, export_id: str) -> dict[str, Any]:
        path: Path | None = None
        quarantined: Path | None = None
        try:
            with self.service.db.transaction(immediate=True) as connection:
                row = connection.execute(
                    "SELECT id, review_set_id, status, questions_path FROM exports WHERE id = ?",
                    (export_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"Export {export_id} was not found")
                if row["status"] == "generating":
                    raise ValidationError("A generating export cannot be deleted")
                if row["questions_path"]:
                    path = self._safe_export_path(row["questions_path"], require_exists=False)
                    if path.is_file():
                        quarantined = self.settings.temp_dir / f"delete-{uuid.uuid4()}.pdf"
                        os.replace(path, quarantined)
                connection.execute(
                    """
                    DELETE FROM idempotency_records
                    WHERE scope = 'create_review_sheet'
                      AND json_extract(response_json, '$.export_id') = ?
                    """,
                    (export_id,),
                )
                connection.execute("DELETE FROM exports WHERE id = ?", (export_id,))
                connection.execute("DELETE FROM review_sets WHERE id = ?", (row["review_set_id"],))
        except BaseException:
            if quarantined is not None and quarantined.is_file() and path is not None:
                os.replace(quarantined, path)
            raise
        if quarantined is not None:
            quarantined.unlink(missing_ok=True)
        return {"ok": True, "export_id": export_id, "status": "deleted"}

    def read_export(self, export_id: str, booklet: str) -> bytes:
        if booklet != "questions":
            raise ValidationError("Only the questions booklet is available")
        row = self.service.db.fetch_one(
            "SELECT status, questions_path AS path, questions_sha256 AS sha256 FROM exports WHERE id = ?",
            (export_id,),
        )
        if row is None:
            raise NotFoundError(f"Export {export_id} was not found")
        if row["status"] != "ready" or not row["path"]:
            raise NotFoundError(f"The {booklet} booklet is not available")
        path = self._safe_export_path(row["path"])
        if _sha256(path) != row["sha256"]:
            raise ExportError(
                "Export file integrity check failed",
                details={"export_id": export_id, "booklet": booklet},
            )
        return path.read_bytes()

    def _render_export(
        self,
        *,
        export_id: str,
        lease_token: str,
        title: str,
        snapshots: list[dict[str, Any]],
        generated_at: datetime,
    ) -> dict[str, Any]:
        published_paths: list[Path] = []
        try:
            questions_name = f"{export_id}-{lease_token}-questions.pdf"
            questions_relative = f"exports/{questions_name}"
            questions_path = self.settings.data_dir / questions_relative
            questions_html = self._build_html(
                title=title,
                snapshots=snapshots,
                generated_at=generated_at,
            )
            self._html_to_pdf(questions_html, questions_path, f"{export_id}-questions")
            published_paths.append(questions_path)
            result: dict[str, Any] = {
                "questions": self._file_descriptor(
                    export_id, "questions", questions_relative, _sha256(questions_path)
                )
            }
            return result
        except BaseException:
            for published in published_paths:
                published.unlink(missing_ok=True)
            raise

    def _cleanup_abandoned_render_files(self, export_id: str) -> None:
        for path in self.settings.exports_dir.glob(f"{export_id}-*-questions.pdf"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _discard_render_result(self, result: dict[str, Any]) -> None:
        questions = result.get("questions")
        if not isinstance(questions, dict) or not questions.get("relative_path"):
            return
        path = self._safe_export_path(questions["relative_path"], require_exists=False)
        path.unlink(missing_ok=True)

    def _build_html(
        self,
        *,
        title: str,
        snapshots: list[dict[str, Any]],
        generated_at: datetime,
    ) -> str:
        local_time = generated_at.astimezone(self.settings.timezone)
        markdown = SafeMarkdownRenderer()
        parts = [
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
            "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
            "style-src 'unsafe-inline'; img-src data:; font-src data:\">",
            f"<title>{html.escape(title)}</title>",
            f"<style>{CSS.replace('__PDF_FONT__', self.settings.pdf_font)}</style>",
            "</head><body>",
            f"<h1>{html.escape(title)}</h1>",
            "<div class='sheet-meta'>",
            f"<span>{len(snapshots)} 题</span>",
            f"<span>生成时间 {local_time:%Y-%m-%d %H:%M}</span>",
            "</div>",
        ]
        parts.append(
            "<div class='identity'>姓名：____________　日期：____________　得分：____________</div>"
        )
        for snapshot in snapshots:
            number = snapshot["number"]
            context_prefix = f"{number}"
            parts.extend(
                [
                    "<article class='problem'>",
                    "<div class='problem-heading'>",
                    f"<span class='position'>{snapshot['position']}.</span>",
                    f"<span class='problem-number'>{html.escape(number)}</span>",
                    f"<span class='subject'>{html.escape(snapshot['subject'])}</span>",
                    "</div>",
                ]
            )
            parts.append(
                "<div class='markdown'>"
                + markdown.render(snapshot["stem_markdown"], context=f"{context_prefix} stem")
                + "</div>"
            )
            if snapshot["choices"]:
                parts.append("<div class='choices'>")
                for choice in snapshot["choices"]:
                    parts.extend(
                        [
                            "<div class='choice'>",
                            f"<div class='choice-label'>{html.escape(choice['label'])}.</div>",
                            "<div class='markdown'>"
                            + markdown.render(
                                choice["content_markdown"],
                                context=f"{context_prefix} choice {choice['label']}",
                            )
                            + "</div>",
                            "</div>",
                        ]
                    )
                parts.append("</div>")
            area_class = (
                "work-area compact"
                if snapshot["kind"] in {"single_choice", "multiple_choice"}
                else "work-area"
            )
            parts.append(f"<div class='{area_class}'></div>")
            parts.append("</article>")
        parts.append("</body></html>")
        return "".join(parts)

    def _html_to_pdf(self, document: str, target: Path, job_name: str) -> None:
        browser = self.settings.browser_path
        if browser is None or not browser.is_file():
            raise ExportError(
                "No Chromium-based browser was found for PDF rendering",
                details={
                    "code": "PDF_RENDERER_UNAVAILABLE",
                    "fix": "Set ERRORBOOK_BROWSER_PATH to Microsoft Edge, Chrome, or Chromium.",
                },
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"errorbook-{job_name}-",
            dir=self.settings.temp_dir,
            ignore_cleanup_errors=True,
        ) as temporary:
            temp_dir = Path(temporary)
            html_path = temp_dir / "document.html"
            temporary_pdf = temp_dir / "document.pdf"
            profile_dir = temp_dir / "browser-profile"
            html_path.write_text(document, encoding="utf-8")
            command = [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile_dir}",
                f"--print-to-pdf={temporary_pdf}",
                html_path.as_uri(),
            ]
            creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    timeout=90,
                    creationflags=creation_flags,
                )
            except subprocess.TimeoutExpired as exc:
                raise ExportError(
                    "PDF rendering timed out", details={"timeout_seconds": 90}
                ) from exc
            if completed.returncode != 0 or not temporary_pdf.is_file():
                stderr = completed.stderr.decode("utf-8", errors="replace")[-1000:]
                raise ExportError(
                    "Browser failed to render the PDF",
                    details={"return_code": completed.returncode, "stderr": stderr},
                )
            self._validate_pdf(temporary_pdf)
            os.replace(temporary_pdf, target)

    def _validate_pdf(self, path: Path) -> None:
        if path.stat().st_size < 1_000:
            raise ExportError("Generated PDF is unexpectedly small")
        try:
            reader = PdfReader(path)
            if reader.is_encrypted or len(reader.pages) < 1:
                raise ExportError("Generated PDF is encrypted or has no pages")
            first = reader.pages[0].mediabox
            width = float(first.width)
            height = float(first.height)
            if not (580 <= width <= 610 and 825 <= height <= 855):
                raise ExportError(
                    "Generated PDF is not A4",
                    details={"width_points": width, "height_points": height},
                )
        except ExportError:
            raise
        except Exception as exc:
            raise ExportError("Generated PDF could not be validated") from exc

    def _file_descriptor(
        self, export_id: str, booklet: str, relative_path: str, sha256: str
    ) -> dict[str, Any]:
        path = self._safe_export_path(relative_path)
        actual_sha256 = _sha256(path)
        if sha256 and actual_sha256 != sha256:
            raise ExportError(
                "Export file integrity check failed",
                details={"export_id": export_id, "booklet": booklet},
            )
        return {
            "resource_uri": f"errorbook://exports/{export_id}/{booklet}",
            "local_path": str(path),
            "relative_path": relative_path,
            "sha256": actual_sha256,
            "size_bytes": path.stat().st_size,
        }

    def _safe_export_path(self, relative_path: str, *, require_exists: bool = True) -> Path:
        path = (self.settings.data_dir / relative_path).resolve()
        exports_root = self.settings.exports_dir.resolve()
        try:
            path.relative_to(exports_root)
        except ValueError as exc:
            raise ExportError("Stored export path is outside the exports directory") from exc
        if require_exists and not path.is_file():
            raise NotFoundError("Export file is missing from storage")
        return path
