"""Pipeline orchestrator coordinating extraction, classification, fingerprinting, parsing, validation, and storage."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Sequence
import uuid

from src.classifier.classifier import classify_document
from src.classifier.models import ClassificationDecision, ClassificationResult
from src.engine.engine import ParsingEngine
from src.fingerprint.extractor import extract_fingerprint
from src.fingerprint.models import (
    FingerprintMatchResult,
    FingerprintMatchStatus,
    FormatFingerprint,
)
from src.ir.exceptions import PDFExtractionError
from src.ir.extractor import extract_pdf
from src.ir.models import DocumentIR
from src.learner.learner import Learner
from src.learner.models import LearningResult, ProposalStatus
from src.models.enums import DocumentStatus, StatementType
from src.models.models import ParseResult
from src.pipeline.exceptions import (
    DocumentExtractionError,
    PipelineError,
    PipelineStorageError,
)
from src.pipeline.models import (
    BatchPipelineResult,
    PipelineConfig,
    PipelineResult,
)
from src.specs.models import ParsingSpec
from src.specs.store import SpecStore
from src.store.models import DocumentRecord
from src.store.store import StatementStore
from src.validator.validator import Validator


def compute_file_hash(file_path: Path | str) -> str:
    """Compute the SHA-256 digest of a file on disk."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_ir_hash(doc: DocumentIR) -> str:
    """Compute a deterministic SHA-256 digest for an in-memory DocumentIR."""
    hasher = hashlib.sha256()
    full_text = doc.get_all_text()
    if full_text:
        hasher.update(full_text.encode("utf-8"))
    else:
        # Fallback to page count and table count if text is empty
        hasher.update(f"pages:{doc.page_count}".encode("utf-8"))
    return hasher.hexdigest()


class Pipeline:
    """Non-interactive orchestrator for PMS and AIF financial statement ingestion and parsing."""

    def __init__(
        self,
        store: StatementStore | None = None,
        spec_store: SpecStore | None = None,
        engine: ParsingEngine | None = None,
        validator: Validator | None = None,
        learner: Learner | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        """Initialize the pipeline with its processing components and configuration.

        Args:
            store: Persistent SQLite storage for documents, statements, and audits.
            spec_store: Repository for deterministic parsing specifications.
            engine: Deterministic extraction engine mapping IR to domain models.
            validator: Financial rules and reconciliation engine.
            learner: Optional LLM learner for synthesizing new format specifications.
            config: Pipeline operational parameters and thresholds.
        """
        self.config = config or PipelineConfig()
        self.store = store or StatementStore()
        self.spec_store = spec_store or SpecStore()
        self.engine = engine or ParsingEngine()
        self.validator = validator or Validator(config=self.config.validator_config)
        self.learner = learner

    # -----------------------------------------------------------------------
    # Primary Entry Points
    # -----------------------------------------------------------------------

    def process_file(
        self,
        file_path: str | Path,
        password: str | None = None,
    ) -> PipelineResult:
        """Process a document file on disk through the full pipeline.

        Execution steps:
        1. Verify file and calculate SHA-256 content hash.
        2. Deduplication check against persistent store.
        3. PDF extraction with optional password resolution.
        4. Deterministic statement classification (STATEMENT / MAYBE / SKIP).
        5. Layout fingerprint extraction and format matching.
        6. Specification retrieval, drift detection quarantine, or optional learning.
        7. Deterministic parsing engine execution.
        8. Financial reconciliation and schema validation.
        9. Atomic persistence to SQLite store and optional JSON export.

        Args:
            file_path: Path to the statement document file (e.g. PDF).
            password: Optional explicit password for encrypted documents.

        Returns:
            PipelineResult containing the document status and all extracted records.
        """
        path_obj = Path(file_path).resolve()
        if not path_obj.exists():
            return self._build_failed_result(
                file_path=str(path_obj),
                file_hash="",
                error_msg=f"File does not exist: {path_obj}",
            )
        if not path_obj.is_file():
            return self._build_failed_result(
                file_path=str(path_obj),
                file_hash="",
                error_msg=f"Specified path is not a regular file: {path_obj}",
            )

        # 1. Calculate file hash
        try:
            file_hash = compute_file_hash(path_obj)
        except Exception as e:
            return self._build_failed_result(
                file_path=str(path_obj),
                file_hash="",
                error_msg=f"Failed to read file for hash computation: {e}",
            )

        # 2. Check duplicate document
        if self.config.store_results:
            existing_doc = self.store.find_by_file_hash(file_hash)
            if existing_doc is not None:
                self.store.record_processing_attempt(
                    doc_id=existing_doc.id,
                    status=DocumentStatus.DUPLICATE,
                    format_id=existing_doc.format_id,
                    warnings=["Duplicate document content hash already processed."],
                )
                return PipelineResult(
                    document_id=existing_doc.id,
                    status=DocumentStatus.DUPLICATE,
                    file_path=str(path_obj),
                    file_hash=file_hash,
                    statement_type=existing_doc.statement_type,
                    format_id=existing_doc.format_id,
                    is_duplicate=True,
                    warnings=["File content SHA-256 matches an already ingested document."],
                )

        doc_id = f"doc_{file_hash[:16]}"

        # 3. PDF Extraction & Password Flow
        passwords_to_try: list[str | None] = [password] if password else []
        for pwd in self.config.passwords:
            if pwd not in passwords_to_try:
                passwords_to_try.append(pwd)
        if None not in passwords_to_try:
            # Always attempt unencrypted extraction first
            passwords_to_try.insert(0, None)

        doc_ir: DocumentIR | None = None
        needs_password = False
        extraction_error_msg: str | None = None

        for cand_pwd in passwords_to_try:
            try:
                doc_ir = extract_pdf(path_obj, password=cand_pwd)
                needs_password = False
                extraction_error_msg = None
                break
            except PDFExtractionError as err:
                err_str = str(err)
                if "password" in err_str.lower() or "encrypt" in err_str.lower():
                    needs_password = True
                    extraction_error_msg = err_str
                else:
                    extraction_error_msg = err_str
                    break

        if needs_password and doc_ir is None:
            return self._handle_needs_password(
                doc_id=doc_id,
                file_path=str(path_obj),
                file_hash=file_hash,
                error_msg=extraction_error_msg or "Document is encrypted and password was not provided.",
            )

        if doc_ir is None:
            return self._handle_extraction_failure(
                doc_id=doc_id,
                file_path=str(path_obj),
                file_hash=file_hash,
                error_msg=extraction_error_msg or "PDF extraction produced no document content.",
            )

        # 4. Dispatch to common IR processor
        return self._process_extracted_ir(
            doc=doc_ir,
            doc_id=doc_id,
            file_path=str(path_obj),
            file_hash=file_hash,
        )

    def process_document(
        self,
        doc: DocumentIR,
        file_path: str | None = None,
        file_hash: str | None = None,
        doc_id: str | None = None,
    ) -> PipelineResult:
        """Process an in-memory DocumentIR through classification, fingerprinting, parsing, and storage.

        Args:
            doc: Intermediate Representation of the document.
            file_path: Optional synthetic or source file path.
            file_hash: Optional SHA-256 hash (computed from IR if omitted).
            doc_id: Optional document ID override.

        Returns:
            PipelineResult containing the document status and extracted records.
        """
        path_str = file_path or "in_memory_document.pdf"
        hash_str = file_hash or compute_ir_hash(doc)

        # Check duplicate
        if self.config.store_results:
            existing_doc = self.store.find_by_file_hash(hash_str)
            if existing_doc is not None:
                self.store.record_processing_attempt(
                    doc_id=existing_doc.id,
                    status=DocumentStatus.DUPLICATE,
                    format_id=existing_doc.format_id,
                    warnings=["Duplicate document content hash already processed."],
                )
                return PipelineResult(
                    document_id=existing_doc.id,
                    status=DocumentStatus.DUPLICATE,
                    file_path=path_str,
                    file_hash=hash_str,
                    statement_type=existing_doc.statement_type,
                    format_id=existing_doc.format_id,
                    is_duplicate=True,
                    warnings=["File content SHA-256 matches an already ingested document."],
                )

        target_doc_id = doc_id or f"doc_{hash_str[:16]}"
        return self._process_extracted_ir(
            doc=doc,
            doc_id=target_doc_id,
            file_path=path_str,
            file_hash=hash_str,
        )

    def process_batch(
        self,
        file_paths: Sequence[str | Path],
    ) -> BatchPipelineResult:
        """Process multiple files in sequence, ensuring errors in one file do not halt the batch.

        Args:
            file_paths: Collection of document file paths to process.

        Returns:
            BatchPipelineResult aggregating statistics and individual PipelineResults.
        """
        batch_res = BatchPipelineResult(total_documents=len(file_paths))

        for fp in file_paths:
            try:
                res = self.process_file(fp)
            except Exception as exc:
                res = self._build_failed_result(
                    file_path=str(fp),
                    file_hash="",
                    error_msg=f"Unexpected pipeline exception: {exc}",
                )

            batch_res.results.append(res)
            if res.is_duplicate:
                batch_res.duplicates += 1
            elif res.status in (DocumentStatus.PARSED, DocumentStatus.PARSED_WITH_WARNINGS):
                batch_res.successful += 1
            elif res.status in (DocumentStatus.SKIPPED,):
                batch_res.skipped += 1
            elif res.is_quarantined:
                batch_res.quarantined += 1
            else:
                batch_res.failed += 1

        return batch_res

    def reparse_document(
        self,
        doc_id: str,
        format_id: str | None = None,
    ) -> PipelineResult:
        """Reparse an existing document, optionally with an explicit format specification.

        Args:
            doc_id: Identifier of the document to reparse.
            format_id: Optional explicit format ID override.

        Returns:
            PipelineResult reflecting the new parse outcome.
        """
        doc_record = self.store.get_document_or_raise(doc_id)
        path_obj = Path(doc_record.file_path)

        if not path_obj.exists():
            return self._build_failed_result(
                file_path=doc_record.file_path,
                file_hash=doc_record.file_hash,
                error_msg=f"Cannot reparse: original document file missing at {path_obj}",
            )

        # Extract IR
        try:
            doc_ir = extract_pdf(path_obj)
        except Exception as e:
            return self._build_failed_result(
                file_path=doc_record.file_path,
                file_hash=doc_record.file_hash,
                error_msg=f"Extraction failure during reparse: {e}",
            )

        # Determine spec
        spec: ParsingSpec | None = None
        if format_id:
            spec = self.spec_store.get(format_id)
            if spec is None:
                return self._build_failed_result(
                    file_path=doc_record.file_path,
                    file_hash=doc_record.file_hash,
                    error_msg=f"Specified format '{format_id}' not found in SpecStore.",
                )
        else:
            fp = extract_fingerprint(doc_ir)
            spec, _ = self.spec_store.match_spec_by_fingerprint(
                fp,
                match_threshold=self.config.match_threshold,
                near_match_threshold=self.config.near_match_threshold,
            )
            if spec is None:
                return self._build_failed_result(
                    file_path=doc_record.file_path,
                    file_hash=doc_record.file_hash,
                    error_msg="No matching specification found during reparse.",
                )

        # Execute parse, validate, and update store
        parse_res = self.engine.parse(doc_ir, spec)
        val_res = self.validator.validate(parse_res)
        parse_res.status = val_res.final_status

        if self.config.store_results:
            self.store.save_parse_result(doc_id, parse_res, format_id=spec.format_id)
            self.store.save_validation_result(doc_id, val_res)
            self.store.record_processing_attempt(
                doc_id=doc_id,
                status=parse_res.status,
                format_id=spec.format_id,
                error_message="; ".join(parse_res.errors) if parse_res.errors else None,
                warnings=parse_res.warnings,
            )

        return PipelineResult(
            document_id=doc_id,
            status=parse_res.status,
            file_path=doc_record.file_path,
            file_hash=doc_record.file_hash,
            statement_type=spec.statement_type,
            format_id=spec.format_id,
            spec_version_id=spec.version,
            parse_result=parse_res,
            validation_result=val_res,
            warnings=parse_res.warnings,
            errors=parse_res.errors,
        )

    # -----------------------------------------------------------------------
    # Core Orchestration Flow
    # -----------------------------------------------------------------------

    def _process_extracted_ir(
        self,
        doc: DocumentIR,
        doc_id: str,
        file_path: str,
        file_hash: str,
    ) -> PipelineResult:
        """Coordinate classification, fingerprinting, spec matching, engine, and store."""
        warnings: list[str] = []
        errors: list[str] = []

        # ── Step 1: Classification ───────────────────────────────────────────
        cls_result = classify_document(doc)
        statement_type = cls_result.statement_type or StatementType.PMS

        if cls_result.decision == ClassificationDecision.SKIP:
            return self._record_and_return_simple_status(
                doc_id=doc_id,
                status=DocumentStatus.SKIPPED,
                file_path=file_path,
                file_hash=file_hash,
                statement_type=statement_type,
                classification=cls_result,
                warnings=cls_result.reasons,
                metadata={"signals": cls_result.signals},
            )

        if cls_result.decision == ClassificationDecision.MAYBE:
            return self._record_and_return_simple_status(
                doc_id=doc_id,
                status=DocumentStatus.MAYBE,
                file_path=file_path,
                file_hash=file_hash,
                statement_type=statement_type,
                classification=cls_result,
                warnings=cls_result.reasons,
                metadata={"signals": cls_result.signals},
            )

        # ── Step 2: Fingerprint Extraction & Format Matching ────────────────
        fp = extract_fingerprint(doc)
        matched_spec, match_result = self.spec_store.match_spec_by_fingerprint(
            fp,
            match_threshold=self.config.match_threshold,
            near_match_threshold=self.config.near_match_threshold,
        )

        # ── Step 3: Handle Drift Quarantine ─────────────────────────────────
        if match_result.status == FingerprintMatchStatus.DRIFT_SUSPECTED:
            # Project Plan rule: On DRIFT_SUSPECTED: quarantine only — learner is NOT called automatically.
            warnings.extend(match_result.reasons)
            return self._record_and_return_simple_status(
                doc_id=doc_id,
                status=DocumentStatus.DRIFT_SUSPECTED,
                file_path=file_path,
                file_hash=file_hash,
                statement_type=statement_type,
                format_id=match_result.matched_format_id,
                fingerprint=fp,
                fingerprint_match=match_result,
                classification=cls_result,
                warnings=warnings,
                metadata={"near_match_score": match_result.similarity_score},
            )

        # ── Step 4: Handle Unknown Format (Optional Learner or Quarantine) ───
        learning_res: LearningResult | None = None
        if match_result.status == FingerprintMatchStatus.UNKNOWN or matched_spec is None:
            if self.config.enable_learning and self.learner is not None:
                learning_res = self.learner.learn(
                    doc=doc,
                    statement_type=statement_type,
                    auto_activate=self.config.auto_activate_learned_spec,
                )
                if learning_res.status == ProposalStatus.ACCEPTED and learning_res.spec is not None:
                    matched_spec = learning_res.spec
                elif learning_res.status == ProposalStatus.PROPOSED:
                    # Spec proposed but not auto-activated
                    warnings.append(f"Format proposed as draft '{learning_res.format_id}'; requires approval.")
                    return self._record_and_return_simple_status(
                        doc_id=doc_id,
                        status=DocumentStatus.NEEDS_REVIEW,
                        file_path=file_path,
                        file_hash=file_hash,
                        statement_type=statement_type,
                        format_id=learning_res.format_id,
                        fingerprint=fp,
                        fingerprint_match=match_result,
                        classification=cls_result,
                        learning_result=learning_res,
                        warnings=warnings,
                    )
                else:
                    # Learning failed or rejected
                    errors.append(learning_res.error_message or "Specification learning failed after repair loop.")
                    return self._record_and_return_simple_status(
                        doc_id=doc_id,
                        status=DocumentStatus.LEARNING_FAILED,
                        file_path=file_path,
                        file_hash=file_hash,
                        statement_type=statement_type,
                        format_id=learning_res.format_id,
                        fingerprint=fp,
                        fingerprint_match=match_result,
                        classification=cls_result,
                        learning_result=learning_res,
                        errors=errors,
                    )
            else:
                # Unknown format without learner enabled
                warnings.append("Unknown format: no matching specification found in SpecStore.")
                return self._record_and_return_simple_status(
                    doc_id=doc_id,
                    status=DocumentStatus.NEEDS_REVIEW,
                    file_path=file_path,
                    file_hash=file_hash,
                    statement_type=statement_type,
                    fingerprint=fp,
                    fingerprint_match=match_result,
                    classification=cls_result,
                    warnings=warnings,
                )

        assert matched_spec is not None

        # ── Step 5: Deterministic Parsing Engine ─────────────────────────────
        try:
            if learning_res is not None and learning_res.parse_result is not None:
                parse_result = learning_res.parse_result
            else:
                parse_result = self.engine.parse(doc, matched_spec)
        except Exception as err:
            err_msg = f"Parsing engine execution failure: {err}"
            errors.append(err_msg)
            return self._record_and_return_simple_status(
                doc_id=doc_id,
                status=DocumentStatus.FAILED,
                file_path=file_path,
                file_hash=file_hash,
                statement_type=statement_type,
                format_id=matched_spec.format_id,
                spec_version_id=matched_spec.version,
                fingerprint=fp,
                fingerprint_match=match_result,
                classification=cls_result,
                errors=errors,
            )

        # ── Step 6: Validation ───────────────────────────────────────────────
        try:
            if learning_res is not None and learning_res.validation_result is not None:
                val_result = learning_res.validation_result
            else:
                val_result = self.validator.validate(parse_result)
            parse_result.status = val_result.final_status
        except Exception as err:
            val_result = None
            parse_result.status = DocumentStatus.NEEDS_REVIEW
            warnings.append(f"Validator execution issue: {err}")

        # Combine warnings and errors
        warnings.extend(parse_result.warnings)
        errors.extend(parse_result.errors)

        # ── Step 7: Persistence in StatementStore ────────────────────────────
        if self.config.store_results:
            self._save_success_to_store(
                doc_id=doc_id,
                file_path=file_path,
                file_hash=file_hash,
                spec=matched_spec,
                parse_result=parse_result,
                val_result=val_result,
                fingerprint=fp,
                raw_metadata=doc.metadata,
            )

            # Optional JSON export
            if self.config.export_json:
                self._export_statement_json(doc_id)

        return PipelineResult(
            document_id=doc_id,
            status=parse_result.status,
            file_path=file_path,
            file_hash=file_hash,
            statement_type=matched_spec.statement_type,
            format_id=matched_spec.format_id,
            spec_version_id=matched_spec.version,
            classification=cls_result,
            fingerprint=fp,
            fingerprint_match=match_result,
            parse_result=parse_result,
            validation_result=val_result,
            learning_result=learning_res,
            warnings=warnings,
            errors=errors,
            metadata={"total_pages": doc.page_count},
        )

    # -----------------------------------------------------------------------
    # Internal Persistence Helpers
    # -----------------------------------------------------------------------

    def _save_success_to_store(
        self,
        doc_id: str,
        file_path: str,
        file_hash: str,
        spec: ParsingSpec,
        parse_result: ParseResult,
        val_result: Any | None,
        fingerprint: FormatFingerprint,
        raw_metadata: dict,
    ) -> None:
        """Persist parsed statement, validation, and attempt records atomically."""
        existing = self.store.get_document(doc_id)
        if existing is None:
            doc_rec = DocumentRecord(
                id=doc_id,
                file_path=file_path,
                file_hash=file_hash,
                status=parse_result.status,
                statement_type=spec.statement_type,
                format_id=spec.format_id,
                spec_version_id=spec.version,
                fingerprint=fingerprint,
                warnings=parse_result.warnings,
                errors=parse_result.errors,
                raw_metadata=raw_metadata,
            )
            self.store.create_document(doc_rec)
        else:
            self.store.update_document(
                doc_id=doc_id,
                status=parse_result.status,
                statement_type=spec.statement_type,
                format_id=spec.format_id,
                spec_version_id=spec.version,
                fingerprint=fingerprint,
                warnings=parse_result.warnings,
                errors=parse_result.errors,
                raw_metadata=raw_metadata,
            )

        self.store.save_parse_result(doc_id, parse_result, format_id=spec.format_id)

        if val_result is not None:
            self.store.save_validation_result(doc_id, val_result)

        self.store.record_processing_attempt(
            doc_id=doc_id,
            status=parse_result.status,
            format_id=spec.format_id,
            error_message="; ".join(parse_result.errors) if parse_result.errors else None,
            warnings=parse_result.warnings,
        )

    def _record_and_return_simple_status(
        self,
        doc_id: str,
        status: DocumentStatus,
        file_path: str,
        file_hash: str,
        statement_type: StatementType | None = None,
        format_id: str | None = None,
        spec_version_id: str | None = None,
        fingerprint: FormatFingerprint | None = None,
        fingerprint_match: FingerprintMatchResult | None = None,
        classification: ClassificationResult | None = None,
        learning_result: LearningResult | None = None,
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
        metadata: dict | None = None,
    ) -> PipelineResult:
        """Helper to record non-parsed states (e.g. SKIPPED, MAYBE, DRIFT_SUSPECTED, NEEDS_REVIEW)."""
        warn_list = warnings or []
        err_list = errors or []
        meta_dict = metadata or {}

        if self.config.store_results:
            existing = self.store.get_document(doc_id)
            if existing is None:
                doc_rec = DocumentRecord(
                    id=doc_id,
                    file_path=file_path,
                    file_hash=file_hash,
                    status=status,
                    statement_type=statement_type,
                    format_id=format_id,
                    spec_version_id=spec_version_id,
                    fingerprint=fingerprint,
                    warnings=warn_list,
                    errors=err_list,
                    raw_metadata=meta_dict,
                )
                self.store.create_document(doc_rec)
            else:
                self.store.update_document(
                    doc_id=doc_id,
                    status=status,
                    statement_type=statement_type,
                    format_id=format_id,
                    spec_version_id=spec_version_id,
                    fingerprint=fingerprint,
                    warnings=warn_list,
                    errors=err_list,
                    raw_metadata=meta_dict,
                )

            self.store.record_processing_attempt(
                doc_id=doc_id,
                status=status,
                format_id=format_id,
                error_message="; ".join(err_list) if err_list else None,
                warnings=warn_list,
            )

        return PipelineResult(
            document_id=doc_id,
            status=status,
            file_path=file_path,
            file_hash=file_hash,
            statement_type=statement_type,
            format_id=format_id,
            spec_version_id=spec_version_id,
            classification=classification,
            fingerprint=fingerprint,
            fingerprint_match=fingerprint_match,
            learning_result=learning_result,
            warnings=warn_list,
            errors=err_list,
            metadata=meta_dict,
        )

    def _handle_needs_password(
        self,
        doc_id: str,
        file_path: str,
        file_hash: str,
        error_msg: str,
    ) -> PipelineResult:
        """Record and return NEEDS_PASSWORD outcome."""
        return self._record_and_return_simple_status(
            doc_id=doc_id,
            status=DocumentStatus.NEEDS_PASSWORD,
            file_path=file_path,
            file_hash=file_hash,
            errors=[error_msg],
            warnings=["Document is password protected and require user credentials."],
        )

    def _handle_extraction_failure(
        self,
        doc_id: str,
        file_path: str,
        file_hash: str,
        error_msg: str,
    ) -> PipelineResult:
        """Record and return FAILED extraction outcome."""
        return self._record_and_return_simple_status(
            doc_id=doc_id,
            status=DocumentStatus.FAILED,
            file_path=file_path,
            file_hash=file_hash,
            errors=[error_msg],
        )

    def _build_failed_result(
        self,
        file_path: str,
        file_hash: str,
        error_msg: str,
    ) -> PipelineResult:
        """Construct an immediate FAILED result for missing or unreadable files."""
        return PipelineResult(
            document_id=f"doc_{uuid.uuid4().hex[:12]}",
            status=DocumentStatus.FAILED,
            file_path=file_path,
            file_hash=file_hash,
            errors=[error_msg],
        )

    def _export_statement_json(self, doc_id: str) -> None:
        """Write formatted statement JSON to export_dir if configured."""
        if not self.config.export_dir:
            return
        target_dir = Path(self.config.export_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            json_text = self.store.export_statement_json(doc_id)
            target_file = target_dir / f"{doc_id}.json"
            target_file.write_text(json_text, encoding="utf-8")
        except Exception:
            pass
