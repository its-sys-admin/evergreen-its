"""po_materials.po_naming — the canonical PO PDF name/title helpers (2026-07 job-name
convention). One source for all four surfaces (Box file, Smartsheet attachment, emailed
attachment, internal /Title) so they cannot drift."""
from __future__ import annotations

from po_materials import po_naming


class TestPoPdfFilename:
    def test_job_prefixed(self):
        assert (
            po_naming.po_pdf_filename("2026.001.352.0.0", "2023.126 Kendall Solar")
            == "2023.126 Kendall Solar_PO_2026.001.352.0.0.pdf"
        )

    def test_blank_job_falls_back_to_number_only(self):
        assert po_naming.po_pdf_filename("2026.001.352.0.0", "") == "PO 2026.001.352.0.0.pdf"
        assert po_naming.po_pdf_filename("2026.001.352.0.0", None) == "PO 2026.001.352.0.0.pdf"

    def test_whitespace_only_job_falls_back(self):
        assert po_naming.po_pdf_filename("42", "   ") == "PO 42.pdf"

    def test_slash_in_job_is_sanitised(self):
        # safety_naming.job_folder_name turns a path-like '/' into '-' (no nested Box/Smartsheet path)
        assert po_naming.po_pdf_filename("42", "A/B Job") == "A-B Job_PO_42.pdf"


class TestPoAttachmentFilename:
    def test_id_and_original_name_both_present(self):
        assert (
            po_naming.po_attachment_filename("2026.001.2.0.0", 41, "spec.pdf")
            == "PO_2026.001.2.0.0_ATT41_spec.pdf"
        )


class TestChangeOrderChMarker:
    """Operator directive 2026-08-15: change-order FILE NAMES carry a `CH` marker
    (`CH_{number}` in the number segment). Base-number names stay byte-identical —
    the §47 version-on-conflict key of every previously-filed document is unchanged."""

    def test_pdf_filename_co_number_gets_ch_token(self):
        assert (
            po_naming.po_pdf_filename("2026.384.1.0.0-CO1", "2023.126 Kendall Solar")
            == "2023.126 Kendall Solar_PO_CH_2026.384.1.0.0-CO1.pdf"
        )

    def test_pdf_filename_blank_job_co_fallback_gets_ch_token(self):
        assert (
            po_naming.po_pdf_filename("2026.384.1.0.0-CO1", "")
            == "PO CH_2026.384.1.0.0-CO1.pdf"
        )

    def test_pdf_filename_base_number_unchanged(self):
        assert (
            po_naming.po_pdf_filename("2026.384.1.0.0", "2023.126 Kendall Solar")
            == "2023.126 Kendall Solar_PO_2026.384.1.0.0.pdf"
        )

    def test_attachment_filename_co_number_gets_ch_token(self):
        assert (
            po_naming.po_attachment_filename("2026.384.1.0.0-CO1", 41, "spec.pdf")
            == "PO_CH_2026.384.1.0.0-CO1_ATT41_spec.pdf"
        )

    def test_attachment_filename_base_number_unchanged(self):
        assert (
            po_naming.po_attachment_filename("2026.384.1.0.0", 41, "spec.pdf")
            == "PO_2026.384.1.0.0_ATT41_spec.pdf"
        )

    def test_malformed_co_tail_gets_no_ch_token(self):
        # `-COX` is not the CO grammar — no marker rather than a wrong one.
        assert po_naming.po_pdf_filename("2026.384.1.0.0-COX", "") == "PO 2026.384.1.0.0-COX.pdf"

    def test_pdf_title_is_not_a_file_name_no_ch_token(self):
        # The operator scoped the directive to FILE NAMES; /Title metadata is unmarked.
        assert (
            po_naming.po_pdf_title("2026.384.1.0.0-CO1", "Kendall Solar")
            == "Purchase Order 2026.384.1.0.0-CO1 — Kendall Solar"
        )


class TestPoPdfTitle:
    def test_job_appended(self):
        assert (
            po_naming.po_pdf_title("2026.001.352.0.0", "2023.126 Kendall Solar")
            == "Purchase Order 2026.001.352.0.0 — 2023.126 Kendall Solar"
        )

    def test_blank_job_falls_back(self):
        assert po_naming.po_pdf_title("42", "") == "Purchase Order 42"
        assert po_naming.po_pdf_title("42", None) == "Purchase Order 42"
