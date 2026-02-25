import os
import json
import re
from typing import Any, Dict, Iterable, List, Tuple

import requests


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_date_str(s: str) -> bool:
    return bool(_DATE_RE.match(s.strip()))


def _quote_single(s: str) -> str:
    s = s.replace("'", "''")
    return f"'{s}'"


def to_rosetta_str(values: Iterable[Any]) -> str:
    flat: List[str] = []
    for v in values:
        if v is None:
            flat.append("")
        else:
            flat.append(str(v).strip())
    return _quote_single(",".join(flat))


def build_parameter_list(params: Dict[str, Any]) -> str:
    """
    Build Oracle ERP Integration REST ParameterList string.

    Rules (simplified from design):
    - Parameter names must be UPPERCASE.
    - Rosetta tables (_TBL/_TABLE) are comma-separated lists in single quotes.
    - Dates YYYY-MM-DD are single-quoted.
    """
    items: List[str] = []
    for k, v in params.items():
        if v is None:
            continue
        if not isinstance(k, str) or k.upper() != k:
            raise ValueError(f"Parameter name must be UPPERCASE: {k!r}")

        if isinstance(v, (list, tuple)):
            items.append(f"{k}: {to_rosetta_str(v)}")
            continue

        if isinstance(v, str) and (k.endswith("_TBL") or k.endswith("_TABLE")):
            items.append(f"{k}: {to_rosetta_str([p.strip() for p in v.split(',') if p.strip() != ''])}")
            continue

        if isinstance(v, str) and _is_date_str(v):
            items.append(f"{k}: {_quote_single(v.strip())}")
            continue

        items.append(f"{k}: {v}")
    return "{" + ", ".join(items) + "}"


def build_endpoint(base_url: str, api_version: str, handle: str) -> str:
    """
    Build the Fusion ERP Integrations REST endpoint for a given handle.

    Example:
      /fscmRestApi/resources/11.13.18.05/erpintegrations/processTransaction-transferAsset
    """
    base_url = base_url.rstrip("/")
    rel = f"/fscmRestApi/resources/{api_version}/erpintegrations/processTransaction-{handle}"
    return base_url + rel


def call_fusion(
    base_url: str,
    api_version: str,
    jwt_token: str,
    handle: str,
    params: Dict[str, Any],
    verify_ssl: bool = True,
    timeout_seconds: int = 60,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Call a Fusion ERP Integrations REST handle using JWT (Bearer) auth.

    jwt_token should be the raw JWT string; this function will prepend 'Bearer '.
    """
    op_name = f"processTransaction-{handle}"
    payload = {
        "OperationName": op_name,
        "ParameterList": build_parameter_list(params),
    }

    url = build_endpoint(base_url, api_version, handle)

    session = requests.Session()
    session.headers.update(
        {
            "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
            "REST-header-version": "4",
            "ACCEPT": "application/json",
            "Authorization": f"Bearer {jwt_token}",
        }
    )

    resp = session.post(url, json=payload, timeout=timeout_seconds, verify=verify_ssl)
    try:
        raw = resp.json()
    except Exception:
        raise RuntimeError(f"Non-JSON response (status={resp.status_code}): {resp.text[:500]}")

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(raw, indent=2)[:1000]}")

    pl_raw = raw.get("ParameterList")
    pl: Dict[str, Any] = {}
    if isinstance(pl_raw, str) and pl_raw.strip():
        try:
            pl = json.loads(pl_raw)
        except Exception:
            # Some handles may return non-JSON ParameterList; preserve raw string.
            pl = {"_raw": pl_raw}
    elif isinstance(pl_raw, dict):
        pl = pl_raw

    return raw, pl


def main() -> None:
    """
    Standalone test entrypoint for Fusion ERP Integrations (JWT auth).

    Required environment variables:
      FUSION_BASE_URL   e.g. https://your-fusion-host
      FUSION_API_VERSION e.g. 11.13.18.05
      FUSION_JWT        raw JWT token for Authorization: Bearer <token>
    """
    base_url = os.environ.get("FUSION_BASE_URL", "").strip()
    api_version = os.environ.get("FUSION_API_VERSION", "").strip()
    jwt_token = os.environ.get("FUSION_JWT", "").strip()

    if not base_url or not api_version or not jwt_token:
        raise SystemExit(
            "Set FUSION_BASE_URL, FUSION_API_VERSION, and FUSION_JWT in your environment before running."
        )

    # Example: simple getAssetInformation call.
    # Replace with real values for your environment.
    handle = "getAssetInformation"
    params = {
        "P_BOOK_TYPE_CODE": "OPS CORP",  # TODO: replace with real book
        "P_ASSET_NUMBER": "10026",       # TODO: replace with real asset number
    }

    print(f"Calling Fusion handle={handle} ...")
    raw, pl = call_fusion(
        base_url=base_url,
        api_version=api_version,
        jwt_token=jwt_token,
        handle=handle,
        params=params,
        verify_ssl=True,
        timeout_seconds=60,
    )

    print("Raw response JSON (truncated):")
    print(json.dumps(raw, indent=2)[:2000])

    print("\nParsed ParameterList (if JSON):")
    print(json.dumps(pl, indent=2)[:2000])


if __name__ == "__main__":
    main()

