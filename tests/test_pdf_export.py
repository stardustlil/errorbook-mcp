from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from pypdf import PdfReader

from errorbook_mcp.errors import ExportError
from errorbook_mcp.pdf_export import PdfExporter, SafeMarkdownRenderer
from errorbook_mcp.scheduler import iso_utc, utc_now
from errorbook_mcp.schemas import ProblemPatch, ReviewSheetRequest
from errorbook_mcp.service import ErrorbookService

from .helpers import choice_problem, solution_problem


def test_mathml_renderer_supports_common_math_and_escapes_html() -> None:
    renderer = SafeMarkdownRenderer()
    output = renderer.render(
        "<script>alert(1)</script> 计算 $\\frac{1}{2}+\\sqrt{x}$。",
        context="test stem",
    )
    assert "<math" in output
    assert "<script>" not in output
    assert "&lt;script&gt;" in output


@pytest.mark.parametrize(
    "formula",
    [
        r"$\href{file:///etc/passwd}{x}$",
        r"$\style{background:url(file:///secret)}{x}$",
        r"$\includegraphics{secret.png}$",
        r"$\notacommand{x}$",
    ],
)
def test_mathml_renderer_rejects_unsafe_or_unknown_commands(formula: str) -> None:
    with pytest.raises(ExportError):
        SafeMarkdownRenderer().render(formula, context="unsafe stem")


def test_pdf_export_end_to_end_and_snapshot(service: ErrorbookService) -> None:
    if service.settings.browser_path is None:
        pytest.skip("No Chromium-based browser is installed")
    drafts = [
        choice_problem(),
        solution_problem(),
        choice_problem(suffix="（注意正负号）"),
        solution_problem(suffix="并检验所得结果。"),
        choice_problem(suffix="这是用于检查较长中文题干自动换行和选项分页的附加说明。" * 4),
        solution_problem(suffix="请写出每一步消元过程，并说明等价变形的依据。" * 3),
    ]
    created = [
        service.create_problem(draft, idempotency_key=f"pdf-create-{index:04d}")
        for index, draft in enumerate(drafts, start=1)
    ]
    numbers = [result["problem"]["number"] for result in created]
    exporter = PdfExporter(service)
    request = ReviewSheetRequest(
        title="每周错题复习卷",
        mode="numbers",
        numbers=numbers,
        include_answer_booklet=True,
    )
    result = exporter.create_review_sheet(request, idempotency_key="pdf-export-0001")
    assert result["status"] == "ready", result
    assert result["selected_numbers"] == numbers
    for booklet in ("questions", "answers"):
        descriptor = result[booklet]
        path = Path(descriptor["local_path"])
        assert path.is_file()
        assert path.read_bytes().startswith(b"%PDF")
        reader = PdfReader(path)
        assert len(reader.pages) >= 1
        text = "".join(page.extract_text() or "" for page in reader.pages)
        for number in numbers:
            assert number in text
        assert exporter.read_export(result["export_id"], booklet).startswith(b"%PDF")

    replay = exporter.create_review_sheet(request, idempotency_key="pdf-export-0001")
    assert replay["export_id"] == result["export_id"]

    service.update_problem(
        numbers[0],
        ProblemPatch(stem_markdown="后来修正的题干 $y=1$"),
        expected_version=1,
        reason="验证快照",
        idempotency_key="pdf-update-0001",
    )
    row = service.db.fetch_one(
        "SELECT snapshot_json FROM review_set_items WHERE review_set_id = ? AND position = 1",
        (result["review_set_id"],),
    )
    assert row is not None
    assert "后来修正" not in json.loads(row["snapshot_json"])["stem_markdown"]

    questions_path = Path(result["questions"]["local_path"])
    original_pdf = questions_path.read_bytes()
    try:
        questions_path.write_bytes(original_pdf + b"tampered")
        with pytest.raises(ExportError, match="integrity"):
            exporter.read_export(result["export_id"], "questions")
    finally:
        questions_path.write_bytes(original_pdf)


def test_pdf_formula_failure_is_persisted(service: ErrorbookService) -> None:
    if service.settings.browser_path is None:
        pytest.skip("No Chromium-based browser is installed")
    created = service.create_problem(
        solution_problem(suffix=r"$\href{file:///secret}{x}$"),
        idempotency_key="unsafe-create-01",
    )
    exporter = PdfExporter(service)
    result = exporter.create_review_sheet(
        ReviewSheetRequest(mode="numbers", numbers=[created["problem"]["number"]]),
        idempotency_key="unsafe-export-01",
    )
    assert result["status"] == "failed"
    status = exporter.get_export(result["export_id"])
    assert status["status"] == "failed"
    problem = service.get_problem(created["problem"]["number"])["problem"]
    assert problem["last_selected_at"] is None


def test_generating_export_uses_lease_and_recovers(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="lease-create-001")
    number = created["problem"]["number"]
    exporter = PdfExporter(service)
    request = ReviewSheetRequest(mode="numbers", numbers=[number])

    def interrupt(**_: object) -> dict[str, object]:
        raise SystemExit(9)

    monkeypatch.setattr(exporter, "_render_export", interrupt)
    with pytest.raises(SystemExit):
        exporter.create_review_sheet(request, idempotency_key="lease-export-001")
    generating = service.db.fetch_one("SELECT * FROM exports")
    assert generating is not None and generating["status"] == "generating"
    assert service.get_problem(number)["problem"]["last_selected_at"] is None

    def should_not_run(**_: object) -> dict[str, object]:
        raise AssertionError("an active lease must not be stolen")

    monkeypatch.setattr(exporter, "_render_export", should_not_run)
    replay = exporter.create_review_sheet(request, idempotency_key="lease-export-001")
    assert replay["status"] == "generating"

    with service.db.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE exports SET lease_expires_at = ?",
            (iso_utc(utc_now() - timedelta(seconds=1)),),
        )

    def finish(**_: object) -> dict[str, object]:
        return {
            "questions": {
                "relative_path": "exports/recovered.pdf",
                "sha256": "test-sha",
            }
        }

    monkeypatch.setattr(exporter, "_render_export", finish)
    recovered = exporter.create_review_sheet(request, idempotency_key="lease-export-001")
    assert recovered["status"] == "ready"
    assert recovered["export_id"] == generating["id"]
    assert service.get_problem(number)["problem"]["last_selected_at"] is not None


def test_pdf_renderer_has_no_cross_request_formula_counter(service: ErrorbookService) -> None:
    exporter = PdfExporter(service)
    formula_text = " ".join(f"$x_{{{index}}}$" for index in range(150))

    def render(number: str) -> str:
        return exporter._build_html(
            title="并发公式测试",
            snapshots=[
                {
                    "position": 1,
                    "number": number,
                    "kind": "solution",
                    "subject": "数学",
                    "stem_markdown": formula_text,
                    "choices": [],
                    "answer_markdown": None,
                    "solution_markdown": None,
                }
            ],
            generated_at=utc_now(),
            answer_booklet=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(render, ["EB-2026-000001", "EB-2026-000002"]))
    assert all(output.count("<math") == 150 for output in outputs)


def test_answer_failure_removes_published_question_file(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = PdfExporter(service)

    def fail_on_answers(document: str, target: Path, job_name: str) -> None:
        del document
        if job_name.endswith("answers"):
            raise ExportError("answer rendering failed")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.7\n" + b"x" * 2_000)

    monkeypatch.setattr(exporter, "_html_to_pdf", fail_on_answers)
    with pytest.raises(ExportError, match="answer rendering failed"):
        exporter._render_export(
            export_id="cleanup-test",
            title="清理测试",
            snapshots=[
                {
                    "position": 1,
                    "number": "EB-2026-000001",
                    "kind": "solution",
                    "subject": "数学",
                    "stem_markdown": "$x=1$",
                    "choices": [],
                    "answer_markdown": "$1$",
                    "solution_markdown": None,
                }
            ],
            include_answer_booklet=True,
            generated_at=utc_now(),
        )
    assert list(service.settings.exports_dir.glob("cleanup-test-*.pdf")) == []
