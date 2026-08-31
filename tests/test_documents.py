"""Office document extraction, and the header-vs-body lesson it re-taught."""

from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile

import pytest

from cute_web_scraper.scraper import (
    _decode_body,
    _office_kind,
    _rows_to_markdown,
    _text_from_document,
)


class FakeResponse:
    """Mimics httpx: some call sites only ever provide .text."""

    def __init__(self, text: str = "", content: bytes = b"") -> None:
        self.text = text
        self.content = content


def _zip_with(*names: str) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<xml/>")
    return buffer.getvalue()


DOCX_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_declared_office_types_are_recognised():
    assert _office_kind(DOCX_CT) == "docx"
    assert _office_kind(XLSX_CT) == "xlsx"
    assert _office_kind("application/msword") == "docx"


def test_html_is_not_mistaken_for_a_document():
    assert _office_kind("text/html") == ""
    assert _office_kind("application/json") == ""


def test_a_spreadsheet_served_as_octet_stream_is_still_found():
    """Microsoft's own download link serves .xlsx as application/octet-stream.

    The content type is a claim; the archive's entries are the evidence.
    """
    assert _office_kind("application/octet-stream", _zip_with("xl/workbook.xml")) == "xlsx"
    assert _office_kind("application/octet-stream", _zip_with("word/document.xml")) == "docx"


def test_an_unrelated_zip_is_not_an_office_document():
    assert _office_kind("application/octet-stream", _zip_with("notes.txt")) == ""


def test_corrupt_bytes_do_not_raise():
    assert _office_kind("application/octet-stream", b"PK\x03\x04not-a-zip") == ""


def test_decoding_html_never_reads_raw_bytes():
    """Regression: sniffing every response broke callers that only set .text."""
    assert _decode_body(FakeResponse(text="<p>hi</p>"), "text/html") == "<p>hi</p>"


def test_decoded_document_text_survives_a_generic_content_type():
    """Regression: the extracted spreadsheet was blanked one call after extraction.

    `_decode_body` had already turned it into a table; re-checking the untrusted
    header here threw that away.
    """
    table = "| a |\n|---|\n| 1 |"
    assert _text_from_document(table, "application/octet-stream") == table


def test_binary_that_is_not_a_document_yields_nothing():
    assert _decode_body(FakeResponse(content=b"\x89PNG\r\n"), "image/png") == ""


@pytest.mark.parametrize(
    ("rows", "expected_lines"),
    [([["a", "b"], ["1", "2"]], 3), ([["only"]], 2)],
)
def test_rows_render_as_a_markdown_table(rows, expected_lines):
    assert len(_rows_to_markdown(rows).splitlines()) == expected_lines


def test_ragged_rows_are_padded_not_dropped():
    out = _rows_to_markdown([["a", "b", "c"], ["1"]])
    assert out.splitlines()[-1] == "| 1 |  |  |"


def test_no_rows_is_empty_not_a_broken_table():
    assert _rows_to_markdown([]) == ""
