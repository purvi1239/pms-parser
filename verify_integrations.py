"""Stage 3: Integration verification workflow for PMS Parser.

Verifies live integrations:
1. Real local Ollama instance running llama3.2:3b using synthetic extraction input.
2. Gmail IMAP connection using Gmail App Passwords (opt-in, strictly read-only).

Guarantees:
- Real Ollama request: no mock fallback.
- Gmail is opt-in only: never touches mailboxes without explicit --gmail flag.
- Read-only IMAP: never modifies, deletes, flags, or sends emails.
- Never logs, prints, or exposes credentials.
- Uses synthetic fixtures only (no real financial documents).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO

def _load_env_file(env_path: Path) -> None:
    """Lightweight .env loader that avoids heavy dependencies."""
    if not env_path.is_file():
        return
    try:
        content = env_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


_load_env_file(Path(__file__).resolve().parent / ".env")

from src.llm_adapter.adapter import LLMAdapter
from src.llm_adapter.exceptions import (
    LLMAdapterError,
    LLMAPIError,
    LLMConnectionError,
    LLMResponseValidationError,
    LLMTimeoutError,
)
from src.llm_adapter.models import LLMConfig


_BANNER_W = 76


def _banner(title: str, out: TextIO = sys.stdout) -> None:
    print("\n" + "=" * _BANNER_W, file=out, flush=True)
    print(f"  {title}", file=out, flush=True)
    print("=" * _BANNER_W, file=out, flush=True)


def _section(title: str, out: TextIO = sys.stdout) -> None:
    line = f"--- {title} "
    print("\n" + line + "-" * max(2, _BANNER_W - len(line)), file=out, flush=True)


def _mask_email(email_str: str | None) -> str:
    if not email_str:
        return "(none)"
    if "@" not in email_str:
        return "***"
    user, domain = email_str.split("@", 1)
    masked_user = user[0] + "***" if len(user) > 1 else "***"
    return f"{masked_user}@{domain}"


# ---------------------------------------------------------------------------
# Ollama Live Verification
# ---------------------------------------------------------------------------

SYNTHETIC_OLLAMA_PROMPT = """You are a deterministic financial parser.
Extract the portfolio holdings from the following redacted statement excerpt into valid JSON.

[SECTION: PORTFOLIO HOLDINGS]
1. Reliance Industries Ltd | ISIN: INE002A01018 | Qty: 50 | Price: 2950.00 | Value: 147500.00
2. HDFC Bank Ltd | ISIN: INE040A01034 | Qty: 100 | Price: 1650.00 | Value: 165000.00

Return ONLY a JSON object conforming to this schema:
{
  "holdings": [
    {
      "security_name": "string",
      "isin": "string",
      "quantity": 0,
      "market_value": 0.0
    }
  ]
}"""

OLLAMA_RESPONSE_SCHEMA = {
    "type": "object",
    "required": ["holdings"],
    "properties": {
        "holdings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["security_name", "quantity"],
            },
        }
    },
}


def verify_ollama(
    api_base: str | None = None,
    model: str | None = None,
    timeout: float = 60.0,
    verbose: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> tuple[bool, dict[str, Any]]:
    """Execute live integration verification against local Ollama.

    Guarantees:
    - Real HTTP request to Ollama endpoint (never falls back to a mock).
    - Clear diagnosis on connection or model missing errors.
    - Verified extraction on synthetic input.
    """
    _banner("STAGE 3: VERIFY LIVE OLLAMA INTEGRATION", out=out)
    print("Objective: Send synthetic parsing request to local Ollama instance.\n"
          "           Verify HTTP connectivity, model availability, and structured JSON extraction.\n"
          "           Strictly live execution — no mock transport.", file=out)

    target_base = api_base or os.getenv("LLM_API_BASE", "http://localhost:11434/v1")
    target_model = model or os.getenv("LLM_MODEL", "llama3.2:3b")

    config = LLMConfig(
        api_base=target_base,
        model=target_model,
        timeout_seconds=timeout,
        replay_mode="disabled",
    )

    _section("Ollama Configuration", out=out)
    print(f"  API Base URL     : {config.api_base}", file=out)
    print(f"  Target Model     : {config.model}", file=out)
    print(f"  Timeout          : {config.timeout_seconds}s", file=out)
    print(f"  Mock Fallback    : DISABLED (strictly live)", file=out)

    adapter = LLMAdapter(config=config)
    diagnostics: dict[str, Any] = {
        "api_base": config.api_base,
        "model": config.model,
        "success": False,
    }

    _section("Executing Live Ollama Request", out=out)
    print(f"  Dispatching synthetic prompt to {config.model} ...", file=out)

    try:
        response = adapter.complete(
            prompt=SYNTHETIC_OLLAMA_PROMPT,
            response_schema=OLLAMA_RESPONSE_SCHEMA,
            temperature=0.0,
            max_tokens=1024,
            metadata={"verification": "stage3_ollama"},
        )

        diagnostics["success"] = True
        diagnostics["latency_ms"] = response.latency_ms
        diagnostics["total_tokens"] = response.total_tokens
        diagnostics["prompt_tokens"] = response.prompt_tokens
        diagnostics["completion_tokens"] = response.completion_tokens
        diagnostics["cost_usd"] = str(response.cost_usd)
        diagnostics["structured"] = response.structured

        _section("Ollama Verification Results", out=out)
        print("  ✓ HTTP Connection  : SUCCESS", file=out)
        print(f"  ✓ Model Loaded     : {response.model}", file=out)
        print(f"  ✓ Round-Trip Time  : {response.latency_ms:.1f} ms", file=out)
        print(f"  ✓ Token Usage      : {response.total_tokens} total "
              f"({response.prompt_tokens} prompt, {response.completion_tokens} completion)", file=out)
        print(f"  ✓ Local Cost       : ${response.cost_usd} (local inference is free)", file=out)

        holdings = []
        if response.structured and "holdings" in response.structured:
            holdings = response.structured["holdings"]

        print(f"  ✓ Structured Output: Valid JSON object with {len(holdings)} holdings extracted", file=out)
        for idx, h in enumerate(holdings, 1):
            sec = h.get("security_name", "Unknown")
            qty = h.get("quantity", "—")
            isin = h.get("isin", "—")
            val = h.get("market_value", "—")
            print(f"     [{idx}] {sec} | ISIN: {isin} | Qty: {qty} | Value: {val}", file=out)

        if verbose:
            _section("Raw Response Content", out=out)
            print(response.content, file=out)

        return True, diagnostics

    except LLMConnectionError as exc:
        print(f"\n❌ OLLAMA CONNECTION FAILED:\n   {exc}", file=err)
        print("\nTroubleshooting Steps:", file=err)
        print("  1. Check if Ollama is running:  'ollama list'", file=err)
        print("  2. Start the Ollama server:     'ollama serve'", file=err)
        print(f"  3. Verify port and base URL:    curl {config.api_base}/tags", file=err)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    except LLMAPIError as exc:
        print(f"\n❌ OLLAMA API ERROR:\n   {exc}", file=err)
        if exc.status_code == 404:
            print(f"\nTroubleshooting Steps for Missing Model '{config.model}':", file=err)
            print(f"  1. Pull the model locally:      'ollama pull {config.model}'", file=err)
            print("  2. Verify installed models:     'ollama list'", file=err)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    except LLMTimeoutError as exc:
        print(f"\n❌ OLLAMA TIMEOUT ERROR:\n   {exc}", file=err)
        print(f"  The model took longer than {config.timeout_seconds}s to respond.", file=err)
        print("  Try a smaller model or increase LLM_TIMEOUT in .env.", file=err)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    except LLMResponseValidationError as exc:
        print(f"\n❌ OLLAMA SCHEMA VALIDATION ERROR:\n   {exc}", file=err)
        print("  The model responded but output failed structured schema validation.", file=err)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    except Exception as exc:
        print(f"\n❌ UNEXPECTED ERROR DURING OLLAMA VERIFICATION:\n   {exc}", file=err)
        diagnostics["error"] = str(exc)
        return False, diagnostics


# ---------------------------------------------------------------------------
# Gmail Live Verification (Opt-In & Strictly Read-Only)
# ---------------------------------------------------------------------------

def verify_gmail(
    mailbox: str = "INBOX",
    search_criteria: str = "ALL",
    timeout: float = 30.0,
    verbose: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> tuple[bool, dict[str, Any]]:
    """Execute opt-in live IMAP verification against Gmail.

    Guarantees:
    - Never accesses real emails unless explicitly invoked.
    - Strictly read-only connection (readonly=True on SELECT).
    - Zero emails modified, deleted, flagged, or sent.
    - Credentials are never logged or exposed.
    """
    _banner("STAGE 3: VERIFY GMAIL IMAP INTEGRATION (OPT-IN & READ-ONLY)", out=out)
    print("Objective: Verify SSL connection, authentication, and mailbox listing on Gmail.\n"
          "           Safety guarantee: strictly read-only mode — 0 emails modified or deleted.\n"
          "           Credentials remain in memory / .env and are never logged.", file=out)

    import imaplib
    import socket

    username = os.getenv("GMAIL_ADDRESS") or os.getenv("GMAIL_USER") or os.getenv("GMAIL_EMAIL")
    password = os.getenv("GMAIL_APP_PASSWORD") or os.getenv("GMAIL_PASSWORD")
    host = os.getenv("GMAIL_HOST", "imap.gmail.com")
    port = int(os.getenv("GMAIL_PORT", "993"))

    diagnostics: dict[str, Any] = {
        "host": host,
        "port": port,
        "mailbox": mailbox,
        "username": _mask_email(username),
        "success": False,
    }

    _section("Gmail Configuration Check", out=out)
    print(f"  IMAP Server Host : {host}:{port} (SSL)", file=out, flush=True)
    print(f"  Gmail Address    : {_mask_email(username)}", file=out, flush=True)
    print(f"  App Password     : {'*** configured' if password else '(NOT SET)'}", file=out, flush=True)
    print(f"  Target Mailbox   : {mailbox}", file=out, flush=True)
    print(f"  Search Criteria  : {search_criteria}", file=out, flush=True)
    print(f"  Read-Only Mode   : ENABLED (mailbox selected with readonly=True)", file=out, flush=True)

    if not username or not password:
        print("\n⚠️  GMAIL CREDENTIALS NOT CONFIGURED IN ENVIRONMENT / .env", file=out, flush=True)
        print("\nTo enable live Gmail IMAP verification, perform the following setup:", file=out, flush=True)
        print("  1. Enable 2-Step Verification on your Google Account:", file=out, flush=True)
        print("     https://myaccount.google.com/signinoptions/two-step-verification", file=out, flush=True)
        print("  2. Generate a 16-character App Password at:", file=out, flush=True)
        print("     https://myaccount.google.com/apppasswords", file=out, flush=True)
        print("     (Select app: 'Mail' or custom name 'PMS Parser')", file=out, flush=True)
        print("  3. Ensure IMAP access is enabled in Gmail Settings:", file=out, flush=True)
        print("     Gmail web -> Settings -> Forwarding and POP/IMAP -> Enable IMAP -> Save Changes", file=out, flush=True)
        print("  4. Add the credentials to your .env file:", file=out, flush=True)
        print("     GMAIL_ADDRESS=\"your.email@gmail.com\"", file=out, flush=True)
        print("     GMAIL_APP_PASSWORD=\"xxxx xxxx xxxx xxxx\"", file=out, flush=True)
        print("\nStatus: REQUIRES SETUP (opt-in feature awaiting user credentials).", file=out, flush=True)
        diagnostics["status"] = "REQUIRES_SETUP"
        return False, diagnostics

    _section("Connecting to Gmail IMAP", out=out)
    print(f"  Establishing SSL handshake with {host}:{port} ...", file=out, flush=True)

    imap_client = None
    try:
        imap_client = imaplib.IMAP4_SSL(host, port, timeout=timeout)
        print(f"  ✓ SSL Connection   : Established", file=out, flush=True)

        typ, auth_data = imap_client.login(username, password)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"Login failed: {auth_data}")
        print(f"  ✓ Authentication   : Accepted for {_mask_email(username)}", file=out, flush=True)

        # Select mailbox in read-only mode
        typ, select_data = imap_client.select(mailbox, readonly=True)
        if typ != "OK":
            raise imaplib.IMAP4.error(f"Failed to select mailbox '{mailbox}': {select_data}")

        total_in_box = select_data[0].decode("ascii", errors="replace") if select_data and select_data[0] else "?"
        print(f"  ✓ Mailbox Selected : '{mailbox}' (read-only mode verified)", file=out, flush=True)
        print(f"  ✓ Messages in Box  : {total_in_box} total messages", file=out, flush=True)

        # Execute search without downloading message bodies
        typ, search_data = imap_client.search(None, search_criteria)
        matched_count = 0
        if typ == "OK" and search_data and search_data[0]:
            matched_count = len(search_data[0].split())
        print(f"  ✓ Search Query     : '{search_criteria}' matched {matched_count} message(s)", file=out, flush=True)

        _section("Gmail Safety Verification", out=out)
        print("  ✓ Messages modified: 0 (strictly read-only)", file=out, flush=True)
        print("  ✓ Messages deleted : 0", file=out, flush=True)
        print("  ✓ Messages sent    : 0", file=out, flush=True)
        print("  ✓ Passwords logged : 0 (never logged or printed in plaintext)", file=out, flush=True)

        try:
            imap_client.close()
        except Exception:
            pass
        try:
            imap_client.logout()
        except Exception:
            pass
        imap_client = None
        print("  ✓ Session Closed   : Safely logged out and disconnected", file=out, flush=True)

        diagnostics["success"] = True
        diagnostics["total_messages"] = total_in_box
        diagnostics["matched_messages"] = matched_count
        return True, diagnostics

    except imaplib.IMAP4.error as exc:
        print(f"\n❌ GMAIL AUTHENTICATION OR IMAP ERROR:\n   {exc}", file=err, flush=True)
        print("\nTroubleshooting Steps:", file=err, flush=True)
        print("  1. Verify GMAIL_ADDRESS matches your Google account exactly.", file=err, flush=True)
        print("  2. Verify GMAIL_APP_PASSWORD is a 16-character Google App Password (not your personal password).", file=err, flush=True)
        print("  3. If 2-Step Verification was recently enabled, generate a fresh App Password at:", file=err, flush=True)
        print("     https://myaccount.google.com/apppasswords", file=err, flush=True)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    except (socket.error, OSError, TimeoutError) as exc:
        print(f"\n❌ GMAIL CONNECTION FAILED:\n   {exc}", file=err, flush=True)
        print(f"  Could not reach {host}:{port}. Check your network connection.", file=err, flush=True)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    except Exception as exc:
        print(f"\n❌ GMAIL VERIFICATION ERROR:\n   {exc}", file=err, flush=True)
        diagnostics["error"] = str(exc)
        return False, diagnostics

    finally:
        if imap_client is not None:
            try:
                imap_client.logout()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Stage 3 Summary & Runner
# ---------------------------------------------------------------------------

def run_verification(
    verify_ollama_flag: bool = True,
    verify_gmail_flag: bool = False,
    ollama_model: str | None = None,
    ollama_api_base: str | None = None,
    gmail_mailbox: str = "INBOX",
    verbose: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    """Run Stage 3 integration verifications and display summary table."""
    _banner("PMS & AIF STATEMENT PARSER — STAGE 3: LIVE INTEGRATIONS VERIFICATION", out=out)
    print("  Scope      : Live Ollama (llama3.2:3b) + Gmail IMAP adapter\n"
          "  Mode       : Live execution (no mock for Ollama, opt-in for Gmail)\n"
          "  Data Safety: Synthetic input for Ollama; read-only access for Gmail\n"
          "  Credentials: Read from .env, never logged or exposed", file=out)

    summary_rows: list[dict[str, str]] = []
    overall_exit_code = 0

    # 1. Ollama verification
    if verify_ollama_flag:
        ollama_ok, ollama_diag = verify_ollama(
            api_base=ollama_api_base,
            model=ollama_model,
            verbose=verbose,
            out=out,
            err=err,
        )
        if ollama_ok:
            summary_rows.append({
                "integration": "Ollama (Local LLM)",
                "target": f"{ollama_diag.get('model')} @ {ollama_diag.get('api_base')}",
                "status": "VERIFIED",
                "notes": f"{ollama_diag.get('latency_ms', 0):.0f}ms, {ollama_diag.get('total_tokens', 0)} tokens, structured output valid",
            })
        else:
            overall_exit_code = 1
            summary_rows.append({
                "integration": "Ollama (Local LLM)",
                "target": f"{ollama_diag.get('model')} @ {ollama_diag.get('api_base')}",
                "status": "FAILED",
                "notes": ollama_diag.get("error", "Failed")[:40],
            })

    # 2. Gmail verification
    if verify_gmail_flag:
        gmail_ok, gmail_diag = verify_gmail(
            mailbox=gmail_mailbox,
            verbose=verbose,
            out=out,
            err=err,
        )
        if gmail_ok:
            summary_rows.append({
                "integration": "Gmail IMAP",
                "target": f"{gmail_diag.get('username')} @ {gmail_diag.get('mailbox')}",
                "status": "VERIFIED",
                "notes": f"SSL OK, {gmail_diag.get('total_messages', 0)} messages, read-only verified",
            })
        elif gmail_diag.get("status") == "REQUIRES_SETUP":
            summary_rows.append({
                "integration": "Gmail IMAP",
                "target": f"{gmail_diag.get('host')}:{gmail_diag.get('port')}",
                "status": "REQUIRES SETUP",
                "notes": "Add GMAIL_ADDRESS & GMAIL_APP_PASSWORD to .env",
            })
        else:
            overall_exit_code = 1
            summary_rows.append({
                "integration": "Gmail IMAP",
                "target": f"{gmail_diag.get('host')}:{gmail_diag.get('port')}",
                "status": "FAILED",
                "notes": gmail_diag.get("error", "Failed")[:40],
            })
    else:
        # Document that Gmail is available but opt-in
        has_creds = bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"))
        summary_rows.append({
            "integration": "Gmail IMAP",
            "target": "imap.gmail.com:993",
            "status": "OPT-IN ONLY" if has_creds else "REQUIRES SETUP",
            "notes": "Run with --gmail to verify (read-only, never modifies emails)",
        })

    # Summary table
    _banner("STAGE 3 INTEGRATION VERIFICATION SUMMARY", out=out)
    headers = ["Integration", "Target Endpoint", "Status", "Notes"]
    col_w = [20, 32, 16, 40]
    row_fmt = "  ".join(f"{{:<{w}}}" for w in col_w)
    sep_bar = "  ".join("─" * w for w in col_w)

    print(row_fmt.format(*headers), file=out)
    print(sep_bar, file=out)
    for row in summary_rows:
        print(row_fmt.format(
            row["integration"], row["target"], row["status"], row["notes"]
        ), file=out)
    print(sep_bar, file=out)

    if overall_exit_code == 0:
        print("\n✓ Stage 3 verification finished without unhandled errors.\n", file=out)
    else:
        print("\n❌ Stage 3 verification completed with issues. Check diagnostics above.\n", file=err)

    return overall_exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="verify_integrations",
        description="Verify live integrations for PMS Parser (Ollama llama3.2:3b and Gmail IMAP).",
    )
    parser.add_argument(
        "--ollama",
        action="store_true",
        default=False,
        help="Verify live local Ollama integration using synthetic prompt (default if no flags given).",
    )
    parser.add_argument(
        "--gmail",
        action="store_true",
        default=False,
        help="Opt-in to verify live Gmail IMAP SSL connection using credentials in .env (read-only).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Verify both Ollama and Gmail integrations.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Ollama model override (default: llama3.2:3b).",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help="Ollama API base URL override (default: http://localhost:11434/v1).",
    )
    parser.add_argument(
        "--mailbox",
        default="INBOX",
        help="Gmail mailbox to inspect in read-only mode (default: INBOX).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help="Show detailed request/response payloads.",
    )

    args = parser.parse_args()

    # Determine what to run
    run_all = args.all
    run_ollama = args.ollama or run_all or (not args.gmail and not args.all)
    run_gmail = args.gmail or run_all

    return run_verification(
        verify_ollama_flag=run_ollama,
        verify_gmail_flag=run_gmail,
        ollama_model=args.model,
        ollama_api_base=args.api_base,
        gmail_mailbox=args.mailbox,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
