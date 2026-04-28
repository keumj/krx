from __future__ import annotations

import os
import tempfile
from pathlib import Path


def configure_ssl(insecure_ssl: bool = False, ca_bundle: str | None = None) -> None:
    """Configure process-level SSL settings for environments with strict network policy."""
    bundle = str(ca_bundle).strip() if ca_bundle else ""
    if not bundle and not insecure_ssl:
        default_bundle = Path('data/certs/windows_root_bundle.pem')
        if default_bundle.exists() and default_bundle.is_file():
            bundle = str(default_bundle.resolve())
    if bundle:
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ["REQUESTS_CA_BUNDLE"] = bundle
        os.environ["CURL_CA_BUNDLE"] = bundle
        os.environ["KEUMJ_CA_BUNDLE"] = bundle
        os.environ["KEUMJ_INSECURE_SSL"] = "0"
        return

    if insecure_ssl:
        os.environ["PYTHONHTTPSVERIFY"] = "0"
        os.environ["REQUESTS_CA_BUNDLE"] = ""
        os.environ["CURL_CA_BUNDLE"] = ""
        os.environ["SSL_CERT_FILE"] = ""
        os.environ["KEUMJ_CA_BUNDLE"] = ""
        os.environ["KEUMJ_INSECURE_SSL"] = "1"
        return

    # Keep default verification behavior unless caller requested overrides.
    os.environ["KEUMJ_CA_BUNDLE"] = ""
    os.environ["KEUMJ_INSECURE_SSL"] = "0"


def ensure_writable_dir(path: Path) -> None:
    """Raise PermissionError early if output directory is blocked by security policy."""
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path, delete=True) as tmp:
        tmp.write("write-check")
        tmp.flush()


def security_hint(exc: Exception, output_dir: Path | None = None) -> str | None:
    msg = str(exc)
    lower = msg.lower()

    permission_markers = [
        "permission denied",
        "access is denied",
        "winerror 5",
        "unauthorizedaccessexception",
    ]
    if isinstance(exc, PermissionError) or any(m in lower for m in permission_markers):
        out = f"\n- output dir: {output_dir}" if output_dir else ""
        return (
            "Security/permission error detected.\n"
            "Try these checks:\n"
            "1) Run inside a writable folder (not protected Desktop/Documents policy folder)\n"
            "2) Use a local output path with --out-dir (for example outputs/run_local)\n"
            "3) Allow python.exe in Windows Security -> Ransomware protection -> Controlled folder access\n"
            f"{out}"
        )

    exec_policy_markers = [
        "running scripts is disabled",
        "executionpolicy",
        "is not digitally signed",
    ]
    if any(m in lower for m in exec_policy_markers):
        return (
            "PowerShell security policy blocked script execution.\n"
            "Use a CMD launcher (*.cmd) or run:\n"
            "powershell -ExecutionPolicy Bypass -File <script.ps1>"
        )

    ssl_markers = [
        "ssl",
        "certificate verify failed",
        "self signed certificate",
        "tls",
    ]
    if any(m in lower for m in ssl_markers):
        return (
            "TLS/SSL security error detected.\n"
            "If your network uses SSL inspection, provide a CA bundle path with --ca-bundle.\n"
            "For temporary testing only, use --insecure-ssl."
        )

    market_data_markers = [
        "no price data returned from yfinance",
        "failed download",
        "connectionerror",
        "failed to connect",
        "could not connect to server",
        "curl: (7)",
        "fc.yahoo.com",
        "query1.finance.yahoo.com",
        "query2.finance.yahoo.com",
        "yfinance download failed",
        "too many requests",
        "rate limited",
        "yfratelimiterror",
    ]
    if any(m in lower for m in market_data_markers):
        return (
            "Market data download failed.\n"
            "Try these checks:\n"
            "1) Verify ticker/date range and retry\n"
            "2) Check internet/proxy/firewall access to Yahoo Finance and related provider domains\n"
            "3) If SSL inspection is enabled, set CA bundle path (GUI/--ca-bundle)\n"
            "4) For temporary testing, enable Insecure SSL\n"
            "5) To proceed offline in GUI, enable 'Use sample prices (offline)' or set Local Prices CSV Path"
        )

    return None
