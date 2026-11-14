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
WORKSPACE_DIR = DEMO_DIR / "workspace"


def _populate_sample_repo(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    (target / "src" / "crypto").mkdir(parents=True, exist_ok=True)
    (target / "src" / "auth").mkdir(parents=True, exist_ok=True)
    (target / "src" / "middleware").mkdir(parents=True, exist_ok=True)
    (target / "src" / "routes").mkdir(parents=True, exist_ok=True)
    (target / "src" / "services").mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(parents=True, exist_ok=True)

    (target / "src" / "crypto" / "jwt.py").write_text(
        '"""Cryptographic signature helper for session tokens."""\n\n'
        "def verify_signature(raw_token: str) -> bool:\n"
        '    """Return True when raw_token has a valid signature prefix and length."""\n'
        '    return raw_token.startswith("tk_") and len(raw_token) >= 12\n\n'
        "def decode_claims(raw_token: str) -> dict:\n"
        '    if not verify_signature(raw_token):\n'
        '        return {"valid": False}\n'
        '    return {"valid": True, "sub": raw_token[3:9]}\n',
        encoding="utf-8",
    )

    (target / "src" / "auth" / "session.py").write_text(
        '"""Session management and token validation."""\n'
        "from src.crypto.jwt import decode_claims, verify_signature\n\n"
        "class SessionManager:\n"
        "    def validate_token(self, token: str) -> bool:\n"
        '        """Validate bearer token signature and claims."""\n'
        "        if not verify_signature(token):\n"
        "            return False\n"
        "        claims = decode_claims(token)\n"
        '        return bool(claims.get("valid"))\n',
        encoding="utf-8",
    )

    (target / "src" / "middleware" / "require_auth.py").write_text(
        '"""HTTP authentication middleware."""\n'
        "from src.auth.session import SessionManager\n\n"
        "def require_auth(auth_header: str) -> bool:\n"
        "    mgr = SessionManager()\n"
        "    return mgr.validate_token(auth_header)\n",
        encoding="utf-8",
    )

    (target / "src" / "routes" / "checkout.py").write_text(
        '"""Checkout route handler."""\n'
        "from src.auth.session import SessionManager\n"
        "from src.services.order_engine import calculate_order_total\n\n"
        "def process_checkout(token: str, subtotal: float) -> dict:\n"
        "    mgr = SessionManager()\n"
        "    if not mgr.validate_token(token):\n"
        '        return {"status": 401, "error": "Unauthorized"}\n'
        "    total = calculate_order_total(subtotal)\n"
        '    return {"status": 200, "total": total}\n',
        encoding="utf-8",
    )

    # Create a >320 line file to test TokWiz >300-line AST skeleton interception
    order_lines = [
        '"""Large order calculation engine (>320 lines for AST Skeletonizer testing)."""\n\n',
        "def apply_regional_tax(amount: float, rate: float = 0.08) -> float:\n",
        "    return round(amount * (1.0 + rate), 2)\n\n",
        "def calculate_order_total(subtotal: float) -> float:\n",
        "    if subtotal <= 0:\n",
        "        return 0.0\n",
        "    return apply_regional_tax(subtotal, 0.08)\n\n",
    ]
    for i in range(1, 70):
        order_lines.append(
            f"def pricing_rule_{i}(base_amount: float) -> float:\n"
            f'    """Synthetic pricing rule #{i}."""\n'
            f"    discount = {i} * 0.05\n"
            f"    return max(0.0, round(base_amount - discount, 2))\n\n"
        )
    (target / "src" / "services" / "order_engine.py").write_text(
        "".join(order_lines), encoding="utf-8"
    )

    # Create a synthetic lockfile to test guardrail blocking
    lock_entries = ",\n".join(
        f'    "pkg-{i}": {{"version": "1.0.{i}", "resolved": "https://registry.npmjs.org/pkg-{i}"}}'
        for i in range(1, 150)
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


def main() -> int:
    baseline_dir = WORKSPACE_DIR / "project-baseline"
    tokwiz_dir = WORKSPACE_DIR / "project-tokwiz"

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
