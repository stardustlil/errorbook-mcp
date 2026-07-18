from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
from pypdf import PdfReader, PdfWriter

from errorbook_mcp.errors import ExportError, NotFoundError, ValidationError
from errorbook_mcp.pdf_export import PdfExporter, SafeMarkdownRenderer, _sha256
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


def test_mathml_renderer_supports_tex_delimiters() -> None:
    renderer = SafeMarkdownRenderer()
    output = renderer.render(r"\[I=\iint_D e^{\max{x^2,y^2}}\,dx\,dy\]", context="test stem")
    assert output.count("<math") == 1
    assert r"\iint" not in output


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
    )
    result = exporter.create_review_sheet(request, idempotency_key="pdf-export-0001")
    assert result["status"] == "ready", result
    assert result["selected_numbers"] == numbers
    descriptor = result["questions"]
    path = Path(descriptor["local_path"])
    assert path.is_file()
    assert path.read_bytes().startswith(b"%PDF")
    reader = PdfReader(path)
    assert len(reader.pages) >= 1
    text = "".join(page.extract_text() or "" for page in reader.pages)
    for number in numbers:
        assert number in text
    assert exporter.read_export(result["export_id"], "questions").startswith(b"%PDF")

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
    assert "last_selected_at" not in problem


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
    assert "last_selected_at" not in service.get_problem(number)["problem"]

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
    assert "last_selected_at" not in service.get_problem(number)["problem"]


def test_stale_renderer_cannot_overwrite_recovered_export(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="race-create-001")
    request = ReviewSheetRequest(mode="numbers", numbers=[created["problem"]["number"]])
    stale_exporter = PdfExporter(service)
    current_exporter = PdfExporter(service)
    stale_started = Event()
    release_stale = Event()
    stale_path: list[Path] = []

    def render_result(exporter: PdfExporter, export_id: str, lease_token: str, body: bytes):
        relative_path = f"exports/{export_id}-{lease_token}-questions.pdf"
        path = service.settings.data_dir / relative_path
        path.write_bytes(body)
        return {
            "questions": exporter._file_descriptor(
                export_id, "questions", relative_path, _sha256(path)
            )
        }

    def render_stale(*, export_id: str, lease_token: str, **_: object):
        stale_started.set()
        assert release_stale.wait(timeout=10)
        result = render_result(stale_exporter, export_id, lease_token, b"stale-pdf")
        stale_path.append(Path(result["questions"]["local_path"]))
        return result

    def render_current(*, export_id: str, lease_token: str, **_: object):
        return render_result(current_exporter, export_id, lease_token, b"current-pdf")

    monkeypatch.setattr(stale_exporter, "_render_export", render_stale)
    monkeypatch.setattr(current_exporter, "_render_export", render_current)
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_future = executor.submit(
            stale_exporter.create_review_sheet,
            request,
            idempotency_key="race-export-001",
        )
        assert stale_started.wait(timeout=10)
        with service.db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE exports SET lease_expires_at = ?",
                (iso_utc(utc_now() - timedelta(seconds=1)),),
            )
        current = current_exporter.create_review_sheet(request, idempotency_key="race-export-001")
        release_stale.set()
        stale = stale_future.result(timeout=10)

    current_path = Path(current["questions"]["local_path"])
    assert stale["export_id"] == current["export_id"]
    assert current_path.read_bytes() == b"current-pdf"
    assert current_exporter.read_export(current["export_id"], "questions") == b"current-pdf"
    assert stale_path and not stale_path[0].exists()
    assert list(service.settings.exports_dir.glob(f"{current['export_id']}-*-questions.pdf")) == [
        current_path
    ]


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
                }
            ],
            generated_at=utc_now(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(render, ["EB-2026-000001", "EB-2026-000002"]))
    assert all(output.count("<math") == 150 for output in outputs)


def test_export_files_can_be_listed_and_deleted(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="files-create-001")
    exporter = PdfExporter(service)

    def fake_pdf(document: str, target: Path, job_name: str) -> None:
        del document, job_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.7\n" + b"x" * 2_000)

    monkeypatch.setattr(exporter, "_html_to_pdf", fake_pdf)
    result = exporter.create_review_sheet(
        ReviewSheetRequest(mode="numbers", numbers=[created["problem"]["number"]]),
        idempotency_key="files-export-001",
    )
    path = Path(result["questions"]["local_path"])
    listed = exporter.list_exports()
    assert listed["items"][0]["export_id"] == result["export_id"]
    assert listed["items"][0]["questions"]["exists"] is True

    deleted = exporter.delete_export(result["export_id"])
    assert deleted["status"] == "deleted"
    assert not path.exists()
    with pytest.raises(NotFoundError):
        exporter.get_export(result["export_id"])


def test_delete_export_restores_file_when_transaction_rolls_back(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="rollback-create-001")
    exporter = PdfExporter(service)

    def fake_pdf(document: str, target: Path, job_name: str) -> None:
        del document, job_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.7\n" + b"x" * 2_000)

    monkeypatch.setattr(exporter, "_html_to_pdf", fake_pdf)
    result = exporter.create_review_sheet(
        ReviewSheetRequest(mode="numbers", numbers=[created["problem"]["number"]]),
        idempotency_key="rollback-export-001",
    )
    path = Path(result["questions"]["local_path"])
    original_transaction = service.db.transaction

    @contextmanager
    def failing_transaction(*, immediate: bool = False):
        with original_transaction(immediate=immediate) as connection:
            yield connection
            if immediate:
                raise sqlite3.OperationalError("forced commit failure")

    monkeypatch.setattr(service.db, "transaction", failing_transaction)
    with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
        exporter.delete_export(result["export_id"])

    assert path.is_file()
    assert service.db.fetch_one("SELECT id FROM exports WHERE id = ?", (result["export_id"],))
    assert not list(service.settings.temp_dir.glob("delete-*.pdf"))


def test_stored_export_path_cannot_escape_exports_directory(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="escape-create-001")
    exporter = PdfExporter(service)

    def fake_pdf(document: str, target: Path, job_name: str) -> None:
        del document, job_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"%PDF-1.7\n" + b"x" * 2_000)

    monkeypatch.setattr(exporter, "_html_to_pdf", fake_pdf)
    result = exporter.create_review_sheet(
        ReviewSheetRequest(mode="numbers", numbers=[created["problem"]["number"]]),
        idempotency_key="escape-export-001",
    )
    with service.db.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE exports SET questions_path = '../outside.pdf' WHERE id = ?",
            (result["export_id"],),
        )

    with pytest.raises(ExportError, match="outside the exports directory"):
        exporter.get_export(result["export_id"])


def test_export_api_rejects_invalid_missing_and_generating_requests(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    exporter = PdfExporter(service)
    with pytest.raises(ValidationError, match="limit"):
        exporter.list_exports(limit=0)
    with pytest.raises(ValidationError, match="offset"):
        exporter.list_exports(offset=-1)
    with pytest.raises(NotFoundError):
        exporter.get_export("missing-export")
    with pytest.raises(ValidationError, match="questions booklet"):
        exporter.read_export("missing-export", "answers")
    with pytest.raises(NotFoundError):
        exporter.read_export("missing-export", "questions")

    created = service.create_problem(choice_problem(), idempotency_key="api-create-001")

    def interrupt(**_: object) -> dict[str, object]:
        raise SystemExit(9)

    monkeypatch.setattr(exporter, "_render_export", interrupt)
    with pytest.raises(SystemExit):
        exporter.create_review_sheet(
            ReviewSheetRequest(mode="numbers", numbers=[created["problem"]["number"]]),
            idempotency_key="api-export-001",
        )
    row = service.db.fetch_one("SELECT id FROM exports")
    assert row is not None
    status = exporter.get_export(row["id"])
    assert status["status"] == "generating"
    assert status["lease_expires_at"]
    listed = exporter.list_exports()
    assert listed["items"][0]["questions"]["exists"] is False
    with pytest.raises(ValidationError, match="generating"):
        exporter.delete_export(row["id"])
    with pytest.raises(NotFoundError, match="not available"):
        exporter.read_export(row["id"], "questions")


def test_pdf_validation_and_renderer_availability(
    service: ErrorbookService, tmp_path: Path
) -> None:
    exporter = PdfExporter(service)
    small = tmp_path / "small.pdf"
    small.write_bytes(b"%PDF")
    with pytest.raises(ExportError, match="unexpectedly small"):
        exporter._validate_pdf(small)

    invalid = tmp_path / "invalid.pdf"
    invalid.write_bytes(b"not a pdf" + b"x" * 2_000)
    with pytest.raises(ExportError, match="could not be validated"):
        exporter._validate_pdf(invalid)

    non_a4 = tmp_path / "non-a4.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Padding": "x" * 2_000})
    with non_a4.open("wb") as output:
        writer.write(output)
    with pytest.raises(ExportError, match="not A4"):
        exporter._validate_pdf(non_a4)

    unavailable = PdfExporter(ErrorbookService(replace(service.settings, browser_path=None)))
    with pytest.raises(ExportError, match="No Chromium-based browser"):
        unavailable._html_to_pdf("<html></html>", tmp_path / "output.pdf", "missing")


def test_abandoned_render_files_are_removed(service: ErrorbookService) -> None:
    exporter = PdfExporter(service)
    abandoned = service.settings.exports_dir / "export-id-old-questions.pdf"
    abandoned.write_bytes(b"old")
    exporter._cleanup_abandoned_render_files("export-id")
    assert not abandoned.exists()
    exporter._discard_render_result({})
