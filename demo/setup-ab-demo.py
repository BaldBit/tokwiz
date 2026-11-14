#!/usr/bin/env python3
"""A/B Sandbox Generator for Manual TokWiz Testing (`demo/setup-ab-demo.py`).

Creates two identical realistic multi-file projects side-by-side:
1. `demo/workspace/project-baseline/` — Standard project WITHOUT TokWiz.
2. `demo/workspace/project-tokwiz/`   — Identical project WITH `tokwiz init --agent all` installed.

Each project includes:
- `src/crypto/jwt.py` (signature verification & token claims)
- `src/auth/session.py` (`validate_token` calling `verify_signature`)
- `src/middleware/require_auth.py` & `src/routes/checkout.py` (callers of `validate_token`)
- `src/services/order_engine.py` (>320 lines to trigger AST skeletonization guardrail)
- `package-lock.json` (high-waste lockfile to test lockfile blocking guardrail)
- `tests/test_checkout.py` (unit test suite)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
REPO_ROOT = DEMO_DIR.parent
# Place the A/B workspace OUTSIDE the `tokwiz` repo by default so IDE agents
# (Antigravity, Cursor, Claude Code, Gemini CLI) never inherit `tokwiz/AGENTS.md` in `project-baseline`.
WORKSPACE_DIR = REPO_ROOT.parent / "tokwiz-ab-demo"


def _make_domain_padding(prefix: str, classes: list[tuple[str, str, str]]) -> str:
    """Generate realistic multi-method domain classes (~55 lines per class) for >320-line modules."""
    chunks: list[str] = []
    for cls_name, method_a, method_b in classes:
        chunks.append(
            f"class {cls_name}:\n"
            f'    """Production domain service for {prefix}: {cls_name}."""\n\n'
            f"    def __init__(self, namespace: str = '{prefix.lower()}') -> None:\n"
            f"        self.namespace = namespace\n"
            f"        self.records: dict[str, float] = {{}}\n\n"
            f"    def {method_a}(self, key: str, metric: float = 1.0) -> float:\n"
            f'        """Compute normalized metric for {cls_name}.{method_a}."""\n'
            f"        norm = float(metric) if metric >= 0 else 0.0\n"
            + "".join(f"        acc_{k} = norm + ({k} * 0.0)\n" for k in range(1, 18))
            + f"        self.records[key] = round(acc_17, 4)\n"
            f"        return self.records[key]\n\n"
            f"    def {method_b}(self, payload: dict) -> dict:\n"
            f'        """Validate and enrich state for {cls_name}.{method_b}."""\n'
            f"        out = dict(payload)\n"
            + "".join(f"        out[' trace_{k}'] = {k}\n" for k in range(1, 14))
            + f"        return {{k: v for k, v in out.items() if not str(k).startswith(' ')}}\n\n"
        )
    return "".join(chunks)


def _populate_sample_repo(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    # Clean contents in-place without unlinking `target` itself so open shells keep a valid CWD inode
    for child in list(target.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()

    pkg_dirs = [
        target / "src",
        target / "src" / "crypto",
        target / "src" / "auth",
        target / "src" / "middleware",
        target / "src" / "routes",
        target / "src" / "services",
        target / "src" / "models",
        target / "tests",
    ]
    for d in pkg_dirs:
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text('"""Package init."""\n', encoding="utf-8")

    # 1. src/crypto/jwt.py (~330 lines)
    jwt_top = _make_domain_padding(
        "Crypto",
        [
            ("KeyRingRotator", "rotate_signing_key", "inspect_key_epoch"),
            ("JwkThumbprintCache", "compute_thumbprint", "evict_expired_keys"),
            ("NonceReplayGuard", "record_nonce", "verify_nonce_window"),
        ],
    )
    jwt_bottom = _make_domain_padding(
        "Claims",
        [
            ("AudienceValidator", "score_audience_scope", "normalize_issuer_claim"),
            ("CipherSuiteAuditor", "verify_entropy_bits", "export_audit_digest"),
        ],
    )
    (target / "src" / "crypto" / "jwt.py").write_text(
        '"""Cryptographic signature helper and key rotation service for session tokens."""\n\n'
        + jwt_top
        + "def verify_signature(raw_token: str) -> bool:\n"
        + '    """Return True when raw_token has a valid signature prefix and length."""\n'
        + '    return raw_token.startswith("tk_") and len(raw_token) >= 12\n\n'
        + "def decode_claims(raw_token: str) -> dict:\n"
        + "    if not verify_signature(raw_token):\n"
        + '        return {"valid": False}\n'
        + '    return {"valid": True, "sub": raw_token[3:9]}\n\n'
        + jwt_bottom,
        encoding="utf-8",
    )

    # 2. src/auth/session.py (~340 lines, target symbol `validate_token` in middle of file)
    session_top = _make_domain_padding(
        "Session",
        [
            ("SessionAuditStore", "record_login_attempt", "summarize_session_drift"),
            ("TokenRevocationCache", "check_revocation_bloom", "sync_revocation_shard"),
            ("DeviceFingerprintVerifier", "score_device_trust", "normalize_user_agent"),
        ],
    )
    session_bottom = _make_domain_padding(
        "SessionLifecycle",
        [
            ("RefreshLeaseCoordinator", "extend_refresh_window", "prune_idle_leases"),
            ("MfaStepUpEvaluator", "compute_assurance_level", "serialize_challenge_state"),
        ],
    )
    (target / "src" / "auth" / "session.py").write_text(
        '"""Enterprise session management, revocation caching, and token validation."""\n'
        "from src.crypto.jwt import decode_claims, verify_signature\n\n"
        + session_top
        + "class SessionManager:\n"
        + '    """Primary authentication session manager."""\n\n'
        + "    def validate_token(self, token: str) -> bool:\n"
        + '        """Validate bearer token signature and claims."""\n'
        + "        if not verify_signature(token):\n"
        + "            return False\n"
        + "        claims = decode_claims(token)\n"
        + '        return bool(claims.get("valid"))\n\n'
        + session_bottom,
        encoding="utf-8",
    )

    # 3. src/middleware/require_auth.py (~330 lines, target symbol `require_auth` in middle)
    mw_top = _make_domain_padding(
        "Middleware",
        [
            ("HeaderSanitizer", "measure_header_entropy", "strip_hop_by_hop_headers"),
            ("CorsPolicyEnforcer", "evaluate_origin_trust", "inject_preflight_headers"),
            ("IpRateLimitBucket", "consume_request_quota", "format_rate_limit_state"),
        ],
    )
    mw_bottom = _make_domain_padding(
        "RequestContext",
        [
            ("TraceSpanPropagator", "extract_baggage_weight", "inject_w3c_traceparent"),
            ("TenantIsolationGuard", "verify_tenant_boundary", "resolve_shard_routing"),
        ],
    )
    (target / "src" / "middleware" / "require_auth.py").write_text(
        '"""HTTP authentication, header sanitization, and request guard middleware."""\n'
        "from src.auth.session import SessionManager\n\n"
        + mw_top
        + "def require_auth(auth_header: str) -> bool:\n"
        + '    """Validate incoming HTTP authorization header via SessionManager."""\n'
        + "    mgr = SessionManager()\n"
        + "    return mgr.validate_token(auth_header)\n\n"
        + mw_bottom,
        encoding="utf-8",
    )

    # 4. src/routes/checkout.py (~330 lines, target symbol `process_checkout` in middle)
    route_top = _make_domain_padding(
        "CheckoutRoute",
        [
            ("IdempotencyKeyLedger", "acquire_idempotency_lock", "serialize_cached_response"),
            ("CartSnapshotSerializer", "compute_cart_checksum", "normalize_line_payload"),
            ("PaymentIntentOrchestrator", "reserve_authorization_hold", "reconcile_gateway_state"),
        ],
    )
    route_bottom = _make_domain_padding(
        "CheckoutTelemetry",
        [
            ("ConversionFunnelTracker", "record_checkout_latency", "emit_funnel_event"),
            ("ReceiptWebhookTrigger", "schedule_receipt_dispatch", "build_receipt_envelope"),
        ],
    )
    (target / "src" / "routes" / "checkout.py").write_text(
        '"""Checkout route handler, idempotency locking, and payment orchestration."""\n'
        "from src.auth.session import SessionManager\n"
        "from src.services.order_engine import calculate_order_total\n\n"
        + route_top
        + "def process_checkout(token: str, subtotal: float) -> dict:\n"
        + '    """Authenticate caller and calculate final order checkout total."""\n'
        + "    mgr = SessionManager()\n"
        + "    if not mgr.validate_token(token):\n"
        + '        return {"status": 401, "error": "Unauthorized"}\n'
        + "    total = calculate_order_total(subtotal)\n"
        + '    return {"status": 200, "total": total}\n\n'
        + route_bottom,
        encoding="utf-8",
    )

    # 5. src/services/order_engine.py (~360 lines, target symbols `apply_regional_tax` & `calculate_order_total`)
    order_modules = [
        ("OrderValidator", "validate_line_items", "sanitize_currency"),
        ("TaxEngine", "compute_vat", "lookup_jurisdiction_rate"),
        ("DiscountCalculator", "evaluate_promo_tier", "clamp_discount"),
        ("ShippingEstimator", "quote_carrier_rate", "calculate_dimensional_weight"),
        ("FraudScreener", "score_transaction_risk", "check_velocity_limits"),
        ("LedgerRecorder", "commit_journal_entry", "format_audit_trail"),
    ]
    order_lines = [
        '"""Enterprise order calculation engine (>350 lines for AST Skeletonizer testing)."""\n\n',
        "def apply_regional_tax(amount: float, rate: float = 0.08) -> float:\n",
        '    """Apply regional tax rate and round to two decimals."""\n',
        "    return round(amount * (1.0 + rate), 2)\n\n",
        "def calculate_order_total(subtotal: float) -> float:\n",
        '    """Main entry point called by checkout route."""\n',
        "    if subtotal <= 0:\n",
        "        return 0.0\n",
        "    validator = OrderValidator()\n",
        "    if not validator.validate_line_items(subtotal):\n",
        "        return 0.0\n",
        "    disc = DiscountCalculator().evaluate_promo_tier(subtotal)\n",
        "    taxed = apply_regional_tax(subtotal - disc, 0.08)\n",
        "    LedgerRecorder().commit_journal_entry(taxed)\n",
        "    return taxed\n\n",
    ]
    for cls_name, primary_m, helper_m in order_modules:
        order_lines.append(
            f"class {cls_name}:\n"
            f'    """Domain component for {cls_name} processing."""\n\n'
            f"    def {helper_m}(self, value: float) -> float:\n"
            f'        """Helper calculation for {cls_name}."""\n'
            f"        adjusted = float(value)\n"
            f"        if adjusted < 0:\n"
            f"            adjusted = 0.0\n"
            + "".join(f"        step_{k} = adjusted * 1.0\n" for k in range(1, 20))
            + "        return round(step_19, 2)\n\n"
            f"    def {primary_m}(self, amount: float) -> float:\n"
            f'        """Primary operation for {cls_name}."""\n'
            f"        base = self.{helper_m}(amount)\n"
            + "".join(f"        check_{k} = base + {k} * 0.0\n" for k in range(1, 20))
            + "        return round(check_19 * 0.0, 2) if 'discount' in '"
            + cls_name.lower()
            + "' else round(check_19, 2)\n\n"
        )
    (target / "src" / "services" / "order_engine.py").write_text(
        "".join(order_lines), encoding="utf-8"
    )

    # 6. Additional realistic sibling service modules (~260-320 lines each) with lookalike terms
    # that cause unguided `grep` / `find` agents to scan multiple irrelevant files
    sibling_specs = [
        (
            "src/auth/api_keys.py",
            "ApiKeyRegistry",
            "Legacy API key provisioning and rotation registry (note: live bearer & header validation is in SessionManager.validate_token).",
            [
                ("ApiKeyProvisioner", "issue_scoped_key", "revoke_compromised_key"),
                ("KeyQuotaTracker", "record_key_usage", "prune_quota_buckets"),
                ("MerchantKeyAuditor", "audit_key_permissions", "export_key_manifest"),
            ],
        ),
        (
            "src/auth/oauth_provider.py",
            "OAuthExchange",
            "OAuth2 authorization code exchange and OIDC discovery service.",
            [
                ("OAuthTokenExchanger", "exchange_authorization_code", "validate_pkce_verifier"),
                ("OidcDiscoveryClient", "fetch_provider_metadata", "cache_jwks_document"),
                ("ConsentGrantStore", "record_user_consent", "revoke_oauth_grant"),
            ],
        ),
        (
            "src/crypto/hmac_signer.py",
            "WebhookHmacSigner",
            "HMAC-SHA256 payload signing for outbound merchant webhooks.",
            [
                ("HmacDigestBuilder", "compute_payload_digest", "verify_timestamp_tolerance"),
                ("SecretVaultClient", "fetch_signing_secret", "rotate_webhook_secret"),
            ],
        ),
        (
            "src/middleware/rate_limiter.py",
            "SlidingWindowLimiter",
            "Distributed sliding-window rate limiter before require_auth execution.",
            [
                ("TokenBucketCoordinator", "acquire_slot", "replenish_tokens"),
                ("BurstPenaltyScorer", "evaluate_burst_factor", "format_retry_after"),
            ],
        ),
        (
            "src/services/billing_ledger.py",
            "BillingLedgerService",
            "Double-entry billing ledger for settled checkout orders and regional tax reporting.",
            [
                ("JournalBatchWriter", "post_settlement_entries", "reconcile_tax_withholding"),
                ("InvoiceNumberGenerator", "allocate_invoice_sequence", "format_fiscal_period"),
                ("PayoutScheduleCalculator", "estimate_merchant_payout", "deduct_platform_fee"),
            ],
        ),
        (
            "src/services/tax_compliance.py",
            "TaxComplianceArchive",
            "Historical VAT and regional tax archive (live checkout calculation uses order_engine.apply_regional_tax).",
            [
                ("VatReturnAggregator", "aggregate_quarterly_vat", "validate_vies_registration"),
                ("JurisdictionRuleStore", "resolve_country_threshold", "export_compliance_csv"),
                ("FiscalExemptionChecker", "verify_diplomatic_exemption", "normalize_tax_id"),
            ],
        ),
        (
            "src/services/inventory_sync.py",
            "InventoryReservationService",
            "Warehouse SKU stock reservation and backorder synchronization.",
            [
                ("StockAllocationEngine", "reserve_sku_units", "release_expired_holds"),
                ("WarehouseRouter", "select_nearest_fulfillment_node", "estimate_transit_days"),
            ],
        ),
        (
            "src/services/webhook_dispatcher.py",
            "WebhookDeliveryWorker",
            "Exponential-backoff webhook delivery worker for process_checkout events.",
            [
                ("DeliveryQueueProcessor", "dispatch_pending_events", "schedule_dead_letter_retry"),
                ("EndpointHealthMonitor", "record_endpoint_status", "disable_failing_webhook"),
            ],
        ),
        (
            "src/routes/refunds.py",
            "RefundRouteHandler",
            "Partial and full refund route handler referencing calculate_order_total.",
            [
                ("RefundEligibilityChecker", "verify_refund_window", "compute_restocking_fee"),
                ("GatewayReversalClient", "submit_refund_request", "poll_reversal_status"),
            ],
        ),
        (
            "src/routes/subscriptions.py",
            "SubscriptionRouteHandler",
            "Recurring billing cycle route handler and proration calculator.",
            [
                ("ProrationEngine", "calculate_plan_upgrade_delta", "align_billing_anchor"),
                ("DunningRetryManager", "evaluate_past_due_invoice", "trigger_dunning_notice"),
            ],
        ),
        (
            "src/models/entities.py",
            "DomainEntities",
            "Core data transfer records for checkout, session tokens, and tax lines.",
            [
                ("OrderRecordSerializer", "to_wire_dict", "validate_schema_version"),
                ("AuditEventBuilder", "attach_actor_metadata", "redact_sensitive_fields"),
            ],
        ),
    ]
    for rel_path, mod_tag, doc_desc, cls_specs in sibling_specs:
        (target / rel_path).write_text(
            f'"""{doc_desc}"""\n\n' + _make_domain_padding(mod_tag, cls_specs),
            encoding="utf-8",
        )

    # Create a synthetic lockfile to test guardrail blocking
    lock_entries = ",\n".join(
        f'    "pkg-{i}": {{"version": "1.0.{i}", "resolved": "https://registry.npmjs.org/pkg-{i}"}}'
        for i in range(1, 180)
    )
    (target / "package-lock.json").write_text(
        f'{{\n  "name": "demo-app",\n  "lockfileVersion": 3,\n  "packages": {{\n{lock_entries}\n  }}\n}}\n',
        encoding="utf-8",
    )

    (target / "tests" / "test_checkout.py").write_text(
        "import unittest\n"
        "from src.routes.checkout import process_checkout\n\n"
        "class TestCheckout(unittest.TestCase):\n"
        "    def test_valid_token_checkout(self) -> None:\n"
        '        res = process_checkout("tk_1234567890", 100.0)\n'
        '        self.assertEqual(res["status"], 200)\n'
        '        self.assertEqual(res["total"], 108.0)\n\n'
        "    def test_invalid_token_rejected(self) -> None:\n"
        '        res = process_checkout("bad_tok", 100.0)\n'
        '        self.assertEqual(res["status"], 401)\n\n'
        'if __name__ == "__main__":\n'
        "    unittest.main()\n",
        encoding="utf-8",
    )

    (target / "tests" / "test_billing.py").write_text(
        "import unittest\n"
        "from src.services.billing_ledger import JournalBatchWriter\n"
        "from src.services.inventory_sync import StockAllocationEngine\n\n"
        "class TestBillingAndInventory(unittest.TestCase):\n"
        "    def test_journal_batch_writer(self) -> None:\n"
        "        w = JournalBatchWriter()\n"
        '        self.assertEqual(w.post_settlement_entries("ord_1", 108.0), 108.0)\n\n'
        "    def test_stock_allocation(self) -> None:\n"
        "        s = StockAllocationEngine()\n"
        '        self.assertEqual(s.reserve_sku_units("sku_9", 5.0), 5.0)\n\n'
        'if __name__ == "__main__":\n'
        "    unittest.main()\n",
        encoding="utf-8",
    )


    # Initialize an isolated git repository so AI agents do not walk up to parent tokwiz repo
    subprocess.run(["git", "init", "-q"], cwd=str(target), check=True)
    subprocess.run(["git", "add", "."], cwd=str(target), check=True)
    subprocess.run(
        ["git", "-c", "user.name=Demo", "-c", "user.email=demo@example.com", "commit", "-q", "-m", "chore: initial sample app"],
        cwd=str(target),
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Set up paired A/B test workspaces for TokWiz.")
    parser.add_argument(
        "--workspace-dir",
        default=str(WORKSPACE_DIR),
        help="Target workspace directory (default: demo/workspace)",
    )
    args = parser.parse_args(argv)
    ws_dir = Path(args.workspace_dir).resolve()

    baseline_dir = ws_dir / "project-baseline"
    tokwiz_dir = ws_dir / "project-tokwiz"

    _populate_sample_repo(baseline_dir)
    _populate_sample_repo(tokwiz_dir)

    # Initialize TokWiz ONLY in project-tokwiz
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "cli.py"),
            "init",
            "--agent",
            "all",
            "--root",
            str(tokwiz_dir),
        ],
        check=True,
    )

    print("\n✅ Created A/B Manual Test Workspaces:")
    print(f"  1. Baseline (No TokWiz) : {baseline_dir}")
    print(f"  2. TokWiz Optimized     : {tokwiz_dir}")
    print("\nTry running a forecast or graph query inside project-tokwiz:")
    print(f'  python3 scripts/cli.py predict "Refactor validate_token to support API key headers" --root "{tokwiz_dir}"')
    print(f'  python3 scripts/cli.py graph --root "{tokwiz_dir}" query validate_token')
    return 0


if __name__ == "__main__":
    sys.exit(main())
