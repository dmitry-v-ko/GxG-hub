"""GxG Hub connector kit (docs/specs/mcp-toolkit-v1.md, plan milestone M1).

Modules: `canonical` (EFF-05), `errors` (MCC-05), `svc_jwt` (AS-06), `exec_token`
(EFF-18 п.1, AS-09…AS-11), `grants` (GR-01…GR-17), `identity` (ADR-0001),
`redact` (SEC-18), `readiness` (GR-12, GR-13).
"""

from ._jwt import TokenError, parse_public_key

__all__ = ["TokenError", "__version__", "parse_public_key"]

__version__: str = "0.0.0"
