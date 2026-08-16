from __future__ import annotations

import base64
import calendar
import json
import re
import struct
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from core.resources import resource_path


CODE_PREFIX = "EAI2"
LEGACY_CODE_PREFIX = "EAI1"
PRODUCT_ID = "engaixt"
LICENSE_TYPE = "monthly_unlimited"
PUBLIC_KEY_PATH = ("assets", "licensing", "monthly_public_key.pem")
_PERIOD_RE = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")
_COMPACT_BASE_YEAR = 2000
_COMPACT_PAYLOAD = struct.Struct(">HBB")
_COMPACT_SIGNING_DOMAIN = b"EngAixt\0monthly_unlimited\0EAI2\0"


class MonthlyLicenseError(ValueError):
    pass


@dataclass(frozen=True)
class MonthlyLicense:
    """A signed shared code whose dates bound redemption, not membership access."""

    period: str
    valid_from: date
    expires_at: date
    duration_months: int
    license_id: str
    key_id: str
    issued_at: str
    raw_code: str

    def is_active(self, today: date) -> bool:
        return self.valid_from <= today <= self.expires_at

    def is_expired(self, today: date) -> bool:
        return today > self.expires_at

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": 2 if self.raw_code.startswith(f"{CODE_PREFIX}.") else 1,
            "product": PRODUCT_ID,
            "license_type": LICENSE_TYPE,
            "period": self.period,
            "valid_from": self.valid_from.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "duration_months": self.duration_months,
            "license_id": self.license_id,
            "key_id": self.key_id,
            "issued_at": self.issued_at,
        }


def normalize_license_code(code: str) -> str:
    return "".join(str(code or "").split())


def decode_monthly_license_code(
    code: str,
    *,
    public_key_path: str | Path | None = None,
) -> MonthlyLicense:
    normalized = normalize_license_code(code)
    parts = normalized.split(".")
    if len(parts) != 3 or parts[0] not in {CODE_PREFIX, LEGACY_CODE_PREFIX}:
        raise MonthlyLicenseError("激活码格式不正确")

    try:
        payload_bytes = _b64url_decode(parts[1])
        signature = _b64url_decode(parts[2])
    except Exception as exc:
        raise MonthlyLicenseError("激活码编码损坏") from exc

    public_path = Path(public_key_path) if public_key_path else resource_path(*PUBLIC_KEY_PATH)
    try:
        public_key = ECC.import_key(public_path.read_text(encoding="ascii"))
        signed_bytes = (
            _COMPACT_SIGNING_DOMAIN + payload_bytes
            if parts[0] == CODE_PREFIX
            else payload_bytes
        )
        eddsa.new(public_key, "rfc8032").verify(signed_bytes, signature)
    except (OSError, ValueError) as exc:
        raise MonthlyLicenseError("激活码签名无效") from exc

    if parts[0] == CODE_PREFIX:
        return _decode_compact_license(payload_bytes, public_key, normalized)

    return _decode_legacy_license(payload_bytes, normalized)


def _decode_compact_license(payload_bytes: bytes, public_key: Any, normalized: str) -> MonthlyLicense:
    if len(payload_bytes) != _COMPACT_PAYLOAD.size:
        raise MonthlyLicenseError("激活码内容损坏")
    month_index, duration_months, flags = _COMPACT_PAYLOAD.unpack(payload_bytes)
    if duration_months != 1 or flags != 0:
        raise MonthlyLicenseError("激活码内容无效")

    year_offset, zero_based_month = divmod(month_index, 12)
    year = _COMPACT_BASE_YEAR + year_offset
    month = zero_based_month + 1
    period = f"{year:04d}-{month:02d}"
    valid_from, expires_at = period_bounds(period)
    return MonthlyLicense(
        period=period,
        valid_from=valid_from,
        expires_at=expires_at,
        duration_months=duration_months,
        license_id=f"monthly-{period}",
        key_id=_public_key_id(public_key),
        issued_at="",
        raw_code=normalized,
    )


def _decode_legacy_license(payload_bytes: bytes, normalized: str) -> MonthlyLicense:
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MonthlyLicenseError("激活码内容损坏") from exc
    if not isinstance(payload, dict):
        raise MonthlyLicenseError("激活码内容无效")

    _validate_payload(payload)
    period = str(payload["period"])
    valid_from = date.fromisoformat(str(payload["valid_from"]))
    expires_at = date.fromisoformat(str(payload["expires_at"]))
    expected_from, expected_expiry = period_bounds(period)
    if valid_from != expected_from or expires_at != expected_expiry:
        raise MonthlyLicenseError("激活码有效期与月份不匹配")

    return MonthlyLicense(
        period=period,
        valid_from=valid_from,
        expires_at=expires_at,
        duration_months=int(payload.get("duration_months", 1)),
        license_id=str(payload["license_id"]),
        key_id=str(payload["key_id"]),
        issued_at=str(payload["issued_at"]),
        raw_code=normalized,
    )


def validate_monthly_license_code(
    code: str,
    *,
    today: date | None = None,
    public_key_path: str | Path | None = None,
) -> MonthlyLicense:
    license_info = decode_monthly_license_code(code, public_key_path=public_key_path)
    current = today or date.today()
    if current < license_info.valid_from:
        raise MonthlyLicenseError(f"该会员码将于 {license_info.valid_from.isoformat()} 开放激活")
    if license_info.is_expired(current):
        raise MonthlyLicenseError(f"该会员码的激活期限已于 {license_info.expires_at.isoformat()} 结束")
    return license_info


def create_monthly_license_code(
    private_key_path: str | Path,
    period: str,
    *,
    issued_at: datetime | None = None,
) -> MonthlyLicense:
    valid_from, expires_at = period_bounds(period)
    now = issued_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    private_path = Path(private_key_path)
    try:
        private_key = ECC.import_key(private_path.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise MonthlyLicenseError(f"无法读取月码私钥: {private_path}") from exc
    if not private_key.has_private():
        raise MonthlyLicenseError("月码私钥文件不包含私钥")

    key_id = _public_key_id(private_key.public_key())
    issued_text = now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    year, month = (int(part) for part in period.split("-"))
    month_index = (year - _COMPACT_BASE_YEAR) * 12 + (month - 1)
    if not 0 <= month_index <= 0xFFFF:
        raise MonthlyLicenseError("会员码月份超出紧凑格式范围")
    payload_bytes = _COMPACT_PAYLOAD.pack(month_index, 1, 0)
    signature = eddsa.new(private_key, "rfc8032").sign(_COMPACT_SIGNING_DOMAIN + payload_bytes)
    code = f"{CODE_PREFIX}.{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
    return MonthlyLicense(
        period=period,
        valid_from=valid_from,
        expires_at=expires_at,
        duration_months=1,
        license_id=f"monthly-{period}",
        key_id=key_id,
        issued_at=issued_text,
        raw_code=code,
    )


def generate_key_pair(private_key_path: str | Path, public_key_path: str | Path) -> tuple[Path, Path]:
    private_path = Path(private_key_path)
    public_path = Path(public_key_path)
    if private_path.exists() or public_path.exists():
        raise MonthlyLicenseError("密钥文件已存在，拒绝覆盖")

    private_key = ECC.generate(curve="Ed25519")
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_text(private_key.export_key(format="PEM"), encoding="ascii")
    public_path.write_text(private_key.public_key().export_key(format="PEM"), encoding="ascii")
    return private_path, public_path


def period_bounds(period: str) -> tuple[date, date]:
    match = _PERIOD_RE.fullmatch(str(period or "").strip())
    if not match:
        raise MonthlyLicenseError("月份格式必须为 YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _validate_payload(payload: dict[str, Any]) -> None:
    if payload.get("schema") != 1:
        raise MonthlyLicenseError("不支持的激活码版本")
    if payload.get("product") != PRODUCT_ID:
        raise MonthlyLicenseError("激活码不属于 EngAixt")
    if payload.get("license_type") != LICENSE_TYPE:
        raise MonthlyLicenseError("激活码不是月度无额度会员码")
    try:
        duration_months = int(payload.get("duration_months", 1) or 0)
    except (TypeError, ValueError) as exc:
        raise MonthlyLicenseError("激活码会员时长无效") from exc
    if duration_months != 1:
        raise MonthlyLicenseError("激活码会员时长无效")
    required = ("period", "valid_from", "expires_at", "license_id", "key_id", "issued_at")
    if any(not str(payload.get(key) or "").strip() for key in required):
        raise MonthlyLicenseError("激活码缺少必要字段")


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _public_key_id(public_key: Any) -> str:
    public_raw = public_key.export_key(format="raw")
    return base64.b32encode(public_raw[:5]).decode("ascii").rstrip("=").lower()


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
