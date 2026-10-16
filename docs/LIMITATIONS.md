# Project Limitations and Future Enhancements

## 1. Overview

The PMS & AIF Statement Parser automates the ingestion, classification, parsing, validation, and storage of Portfolio Management Services (PMS) and Alternative Investment Fund (AIF) statements.

The current implementation includes deterministic parsing for known formats, fingerprint-based format matching, an LLM-assisted learning workflow for unfamiliar layouts, financial validation, duplicate detection, SQLite persistence, and Gmail IMAP integration.

However, the system has limitations in format coverage, document extraction, financial verification, operational usability, and production deployment. This document describes the current limitations and potential future improvements.

## 2. Functional Limitations

### 2.1 Limited Statement Format Coverage

- The parser has been developed and evaluated using synthetic statement formats and a limited set of external PDF documents.
- Real-world PMS and AIF providers may use different layouts, terminology, table structures, and reporting conventions.
- Newly encountered formats may not be recognized or parsed correctly without additional specifications and validation.
- Broader evaluation using statements from multiple providers is required to assess generalization.

**Future enhancement:** Expand the format library and evaluate the parser against a larger, representative collection of real-world statements.

### 2.2 Unknown Formats and Layout Drift

- Unrecognized document layouts may require the format-learning workflow.
- The LLM-assisted workflow can generate draft parsing specifications, but generated specifications are not guaranteed to be correct.
- Draft specifications require validation and appropriate human approval before being used.
- Changes to a previously recognized layout may trigger a drift warning or manual review instead of automatic parsing.
- Fingerprint matching depends on the document features extracted and the configured matching thresholds.

**Future enhancement:** Improve format discovery, drift diagnostics, specification testing, and the workflow for reviewing and approving newly learned formats.

### 2.3 PDF Extraction Limitations

- Extraction quality depends on the structure and quality of the source document.
- Scanned, image-only, blurry, encrypted, or irregularly formatted PDFs may require OCR, a password, or manual intervention.
- Complex tables, merged cells, unusual column ordering, and multi-page layouts can make accurate extraction difficult.
- Incorrect or incomplete source text can affect downstream classification and parsing.

**Future enhancement:** Improve OCR support, table reconstruction, and handling of complex or malformed PDF documents.

### 2.4 Human Review Requirements

- Ambiguous classifications, suspected layout drift, extraction failures, and validation errors may require human intervention.
- The system is not designed to guarantee fully autonomous processing of every statement.
- The current command-line interface does not provide a dedicated graphical dashboard for reviewing and correcting extracted data.

**Future enhancement:** Introduce a review interface that allows users to inspect extracted values, correct errors, approve learned specifications, and track processing decisions.

## 3. Financial Accuracy and Validation Limitations

### 3.1 Validation Is Not a Guarantee of Accuracy

- The validator checks defined financial consistency rules, including applicable cash, value, NAV, quantity-price, and portfolio-weight relationships.
- Configured tolerances accommodate certain rounding differences and variations in financial reporting.
- Validation can detect specified inconsistencies, but it cannot guarantee that every extracted field is correct.
- A statement may contain plausible but incorrect values that satisfy the implemented validation rules.
- Validation alone cannot establish the authenticity of a statement or independently verify its reported financial information.

**Future enhancement:** Expand validation rules, improve field-level confidence reporting, and compare extracted values against independently verified reference data where available.

### 3.2 Provider-Specific Financial Conventions

- PMS and AIF statements may use different terminology, valuation conventions, and reporting structures.
- Financial fields and transaction classifications may require provider-specific interpretation.
- Additional validation and domain-specific rules may be necessary when introducing new statement formats.

**Future enhancement:** Extend provider-specific validation and document the supported financial conventions for each format.

## 4. Storage and Data Export Limitations

### 4.1 SQLite-Based Persistence

- Parsed statement data and related records are persisted in a local SQLite database.
- The current implementation does not provide a centralized database service or a complete multi-user data management interface.
- Production use would require appropriate database backup, recovery, access-control, and retention procedures.

**Future enhancement:** Introduce configurable database deployment options and operational procedures appropriate for larger workloads.

### 4.2 JSON Export

- Dedicated JSON file export is outside the current release scope.
- Parsed statement data is persisted in SQLite and can be accessed through the application's storage layer, including `StatementStore`.
- Users should not assume that the current CLI provides a supported JSON export command.

**Future enhancement:** Add a tested JSON export facility with configurable output directories, clear error handling, and support for duplicate-document workflows.

## 5. LLM and Format-Learning Limitations

- The format-learning workflow depends on the configured LLM being available and returning usable output.
- Model responses can contain incorrect assumptions or incomplete parsing instructions.
- Validation and repair attempts can improve generated specifications but cannot guarantee correctness.
- LLM-assisted learning is intended for unfamiliar layouts rather than replacing deterministic parsing for known formats.
- Processing time and resource consumption depend on the configured model, document complexity, and available hardware.

**Future enhancement:** Improve learning evaluation, measure model-assisted extraction accuracy, and expand regression testing before approving new specifications.

## 6. Gmail Integration Limitations

- Gmail ingestion uses IMAP and requires valid account configuration and authentication.
- Successful connection and authentication do not guarantee successful processing of every email or attachment.
- Attachment formats, encryption, malformed files, and unusual email structures may require additional handling.
- End-to-end reliability across diverse real email messages and attachments requires broader testing.

**Future enhancement:** Expand attachment-level integration tests, improve retry and failure reporting, and add operational monitoring while preserving safe mailbox handling.

## 7. User Interface and Usability

- The current application is primarily command-line based.
- Users need to understand the available commands and interpret processing statuses and diagnostic messages.
- There is no dedicated graphical dashboard for browsing statements, inspecting validation results, managing specifications, or reviewing failures.

**Future enhancement:** Develop a user-friendly interface for document ingestion, processing history, validation review, and specification management.

## 8. Security and Data Privacy Considerations

- Financial statements can contain confidential client and portfolio information.
- Credentials, application passwords, and API keys must be stored securely and excluded from source control.
- Real client statements, sensitive exports, and production database contents must not be committed to the public repository.
- Local LLM inference can reduce the need to transmit documents to an external model service, but it does not by itself guarantee data security.
- Production deployment requires appropriate access controls, encryption where applicable, data-retention policies, and secure backup procedures.

**Future enhancement:** Conduct a security review and establish documented controls for credentials, database access, sensitive document handling, and production data retention.

## 9. Testing and Production Readiness

- Automated tests cover implemented behavior and known scenarios, but cannot represent every possible statement format or operational failure.
- Synthetic documents provide reproducible test cases but do not fully represent the diversity of real financial documents.
- Passing automated tests does not establish a guarantee of financial accuracy or production reliability.
- Broader real-world evaluation is needed before relying on the parser for important financial workflows.
- Production-grade scheduling, monitoring, alerting, disaster recovery, and service-level guarantees are not established by the current implementation.

**Future enhancement:** Expand the real-world evaluation dataset, perform regression and integration testing, measure extraction accuracy, and establish deployment and monitoring procedures.

## 10. Summary of Future Enhancements

The following improvements are potential future directions and are not claims about currently implemented functionality:

1. Expand support for real-world PMS and AIF statement providers.
2. Improve OCR and complex PDF table extraction.
3. Strengthen format recognition, drift detection, and specification approval workflows.
4. Improve field-level confidence reporting and financial validation.
5. Add a tested JSON export facility.
6. Develop a graphical review and administration interface.
7. Improve end-to-end Gmail attachment processing and operational diagnostics.
8. Strengthen security controls, backup and recovery, and data-retention procedures.
9. Expand real-world testing and production monitoring.

## 11. Conclusion

The current project provides a foundation for automated PMS and AIF statement processing using deterministic parsing, controlled LLM-assisted format learning, financial validation, duplicate detection, and persistent storage.

Its primary limitations concern format generalization, document extraction quality, the need for human review in ambiguous cases, and the operational controls required for production use. These limitations define the next stages of development rather than invalidating the functionality implemented in the current version.
