"""Agent 1 - Expense Intelligence: receipt -> structured expense record, and validation of submissions."""
import json
from typing import Optional

from agents.base import BaseAgent, WorkflowState
from intelligence.entity_extractor import extract_receipt_entities
from intelligence.expense_classifier import classify_expense
from services import receipt_service


class ExpenseIntelligenceAgent(BaseAgent):
    name = "expense_agent"
    description = "OCR + spaCy extraction and category classification"

    # ---- used by the upload route, before an expense exists -------------------------------
    def analyse_receipt(self, file_storage, upload_folder: str, max_bytes: int, tesseract_cmd: str = "",
                        keep: bool = True) -> dict:
        """Read a receipt and return a structured, user-correctable record.

        keep=False is used for live previews: the file is analysed in a temp dir and discarded.
        """
        if keep:
            stored = receipt_service.validate_and_store(file_storage, upload_folder, max_bytes)
            return self._structure(stored, tesseract_cmd)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            stored = receipt_service.validate_and_store(file_storage, tmp, max_bytes)
            rec = self._structure(stored, tesseract_cmd)
        rec.pop("receipt_path", None)
        return rec

    def analyse_text(self, text: str) -> dict:
        """Extract from pasted receipt text (no file)."""
        return self._build_record(text, {"method": "pasted-text", "warning": None})

    def _structure(self, stored, tesseract_cmd) -> dict:
        extraction = receipt_service.extract_text(stored.path, stored.ext, tesseract_cmd or None)
        rec = self._build_record(extraction["text"], extraction)
        rec.update(receipt_path=stored.path, receipt_hash=stored.sha256, receipt_ext=stored.ext)
        return rec

    def _build_record(self, text: str, extraction: dict) -> dict:
        ents = extract_receipt_entities(text)
        cls = classify_expense(text, ents["merchant"] or "")
        notes = list(ents["notes"])
        if extraction.get("warning"):
            notes.insert(0, extraction["warning"])
        return {
            "merchant": ents["merchant"], "date": ents["date"], "amount": ents["amount"],
            "currency": ents["currency"], "category": cls["category"],
            "category_confidence": cls["confidence"], "confidence": ents["confidence"],
            "extraction_method": extraction.get("method"), "notes": notes, "raw_text": text[:4000],
        }

    # ---- used by the orchestrator ---------------------------------------------------------
    def run(self, conn, state: WorkflowState) -> dict:
        """Validate the submitted record against what extraction found and flag mismatches."""
        exp = state.expense
        findings, corrected = [], []
        extracted = {}
        if exp.get("extraction_json"):
            try:
                extracted = json.loads(exp["extraction_json"])
            except ValueError:
                findings.append("Stored extraction data was unreadable")
        for key, col in (("merchant", "merchant"), ("date", "expense_date"), ("amount", "amount"),
                         ("category", "category")):
            ev, sv = extracted.get(key), exp.get(col)
            if ev is not None and sv is not None and str(ev).lower() != str(sv).lower():
                corrected.append({"field": key, "extracted": ev, "submitted": sv})
        cls = classify_expense(f"{exp.get('description') or ''} {extracted.get('raw_text', '')}", exp["merchant"])
        suggested = cls["category"]
        category_mismatch = suggested != exp["category"] and cls["confidence"] >= 0.6 and suggested != "Other"
        if category_mismatch:
            findings.append(f"Text suggests '{suggested}' but expense was filed as '{exp['category']}'")
        if not exp.get("receipt_path"):
            findings.append("No receipt attached")
        for c in corrected:
            findings.append(f"Employee changed {c['field']}: '{c['extracted']}' -> '{c['submitted']}'")
        return {
            "merchant": exp["merchant"], "date": exp["expense_date"], "amount": exp["amount"],
            "currency": exp["currency"], "category": exp["category"],
            "suggested_category": suggested, "category_confidence": cls["confidence"],
            "category_mismatch": category_mismatch, "has_receipt": bool(exp.get("receipt_path")),
            "employee_corrections": corrected, "findings": findings,
            "explanation": "; ".join(findings) if findings else "Receipt data is consistent with the submitted expense.",
        }
