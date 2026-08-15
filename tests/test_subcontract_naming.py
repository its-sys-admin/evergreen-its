"""subcontracts.subcontract_naming — the canonical Subcontract document name helpers
(job-prefixed convention + the 2026-08-15 change-order `CH` file-name marker). One
source for every surface a rendered document's name lives on so they cannot drift."""
from __future__ import annotations

from subcontracts import subcontract_naming

_JOB = "2023.126 Kendall Solar"
_BASE = "2026.384.1.0.0"
_CO = "2026.384.1.0.0-CO1"


class TestBaseNumberNames:
    """Base-number names — byte-identical to the pre-CH-marker convention (the §47
    version-on-conflict key of every previously-filed document is unchanged)."""

    def test_docx_job_prefixed(self):
        assert (
            subcontract_naming.sc_docx_filename(_BASE, _JOB)
            == f"{_JOB}_Subcontract_{_BASE}.docx"
        )

    def test_xlsx_job_prefixed(self):
        assert (
            subcontract_naming.sc_xlsx_filename(_BASE, _JOB)
            == f"{_JOB}_Schedule of Values_{_BASE}.xlsx"
        )

    def test_exhibit_job_prefixed(self):
        assert (
            subcontract_naming.sc_exhibit_filename(_BASE, _JOB)
            == f"{_JOB}_Exhibit A_{_BASE}.docx"
        )

    def test_package_zip_job_prefixed(self):
        assert (
            subcontract_naming.sc_package_zip_filename(_BASE, _JOB)
            == f"{_JOB}_Subcontract Package_{_BASE}.zip"
        )

    def test_pdf_job_prefixed(self):
        assert (
            subcontract_naming.sc_pdf_filename(_BASE, _JOB)
            == f"{_JOB}_Subcontract_{_BASE}.pdf"
        )

    def test_blank_job_falls_back_to_number_only(self):
        assert subcontract_naming.sc_docx_filename(_BASE, "") == f"Subcontract {_BASE}.docx"
        assert subcontract_naming.sc_pdf_filename(_BASE, None) == f"Subcontract {_BASE}.pdf"


class TestChangeOrderChMarker:
    """Operator directive 2026-08-15: change-order FILE NAMES carry a `CH` marker
    (`CH_{number}` in the number segment) across ALL FIVE builders. Zero CO documents
    were filed before this landed, so no existing name shifts."""

    def test_docx_co_number_gets_ch_token(self):
        assert (
            subcontract_naming.sc_docx_filename(_CO, _JOB)
            == f"{_JOB}_Subcontract_CH_{_CO}.docx"
        )

    def test_xlsx_co_number_gets_ch_token(self):
        assert (
            subcontract_naming.sc_xlsx_filename(_CO, _JOB)
            == f"{_JOB}_Schedule of Values_CH_{_CO}.xlsx"
        )

    def test_exhibit_co_number_gets_ch_token(self):
        assert (
            subcontract_naming.sc_exhibit_filename(_CO, _JOB)
            == f"{_JOB}_Exhibit A_CH_{_CO}.docx"
        )

    def test_package_zip_co_number_gets_ch_token(self):
        assert (
            subcontract_naming.sc_package_zip_filename(_CO, _JOB)
            == f"{_JOB}_Subcontract Package_CH_{_CO}.zip"
        )

    def test_pdf_co_number_gets_ch_token(self):
        assert (
            subcontract_naming.sc_pdf_filename(_CO, _JOB)
            == f"{_JOB}_Subcontract_CH_{_CO}.pdf"
        )

    def test_blank_job_co_fallback_gets_ch_token(self):
        assert subcontract_naming.sc_docx_filename(_CO, "") == f"Subcontract CH_{_CO}.docx"

    def test_malformed_co_tail_gets_no_ch_token(self):
        # `-COX` is not the CO grammar — no marker rather than a wrong one.
        assert (
            subcontract_naming.sc_docx_filename("2026.384.1.0.0-COX", _JOB)
            == f"{_JOB}_Subcontract_2026.384.1.0.0-COX.docx"
        )

    def test_pdf_title_is_not_a_file_name_no_ch_token(self):
        # The operator scoped the directive to FILE NAMES; /Title metadata is unmarked.
        assert (
            subcontract_naming.sc_pdf_title(_CO, "Kendall Solar")
            == f"Subcontract {_CO} — Kendall Solar"
        )
