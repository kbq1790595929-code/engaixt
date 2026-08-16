from __future__ import annotations

import calendar
import hashlib
import email.utils
import json
import os
import platform
import sys
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any

from core.monthly_license import (
    LICENSE_TYPE as SIGNED_MONTHLY_LICENSE_TYPE,
    MonthlyLicenseError,
    decode_monthly_license_code,
    normalize_license_code,
)
from core.resources import app_root


TRIAL_QUOTA_CNY = 20.0
TRIAL_QUOTA_RESET_POLICY = "monthly"
EDITION_ENV = "ENGAIXT_EDITION"
LEGACY_EDITION_ENV = "GAME_TRANSLATOR_EDITION"
STATE_DIR_ENV = "ENGAIXT_LICENSE_STATE_DIR"
TRIAL_EDITION = "trial"
UNLIMITED_EDITION = "unlimited"
UNLIMITED_EDITION_ALIASES = {"unlimited", "pro", "paid", "full"}
MONTHLY_UNLIMITED_LICENSE = "monthly_unlimited"
ONLINE_FIRST_LAUNCH = "online_first_launch"
OFFLINE_MONTHLY_CODE = "offline_monthly_code"
LICENSE_TODAY_ENV = "ENGAIXT_LICENSE_TODAY"
_STATE_FILE = "license_state.json"
_APP_NAME = "Engaixt"
_CHECKSUM_SALT = "engaixt-local-trial-v1"
_STATE_LOCK = threading.RLock()
_ONLINE_TIME_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://api.deepseek.com/",
    "https://www.baidu.com/",
    "https://ifdian.net/",
]


class TrialQuotaExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class TrialStatus:
    edition: str
    quota_cny_total: float
    quota_cny_used: float
    quota_cny_remaining: float
    quota_exceeded: bool
    machine_id: str
    state_paths: list[str]
    quota_period: str = ""
    quota_resets_at: str = ""
    quota_reset_policy: str = TRIAL_QUOTA_RESET_POLICY
    license_expires_at: str = ""
    license_activated_at: str = ""
    license_expired: bool = False
    license_source: str = ""
    license_online_required: bool = False
    license_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        remaining: float | None
        remaining = None if self.quota_cny_remaining == float("inf") else self.quota_cny_remaining
        return {
            "edition": self.edition,
            "quota_cny_total": self.quota_cny_total,
            "quota_cny_used": self.quota_cny_used,
            "quota_cny_remaining": remaining,
            "quota_exceeded": self.quota_exceeded,
            "machine_id": self.machine_id,
            "state_paths": self.state_paths,
            "quota_period": self.quota_period,
            "quota_resets_at": self.quota_resets_at,
            "quota_reset_policy": self.quota_reset_policy,
            "license_expires_at": self.license_expires_at,
            "license_activated_at": self.license_activated_at,
            "license_expired": self.license_expired,
            "license_source": self.license_source,
            "license_online_required": self.license_online_required,
            "license_error": self.license_error,
        }


@dataclass(frozen=True)
class EditionInfo:
    edition: str
    expires_at: str = ""
    activated_at: str = ""
    expired: bool = False
    source: str = ""
    license_type: str = ""
    activation_mode: str = ""
    duration_months: int = 0
    duration_days: int = 0
    issued_at: str = ""
    sponsor_id: str = ""
    license_id: str = ""
    online_required: bool = False
    error: str = ""


def current_edition() -> str:
    return current_edition_info().edition


def current_edition_info() -> EditionInfo:
    raw = (os.environ.get(EDITION_ENV) or os.environ.get(LEGACY_EDITION_ENV) or "").strip().lower()
    normalized = _normalize_edition(raw)
    if normalized:
        expires_at = str(os.environ.get("ENGAIXT_EDITION_EXPIRES_AT") or "").strip()
        return _apply_expiry(EditionInfo(normalized, expires_at=expires_at, source="env"))
    signed_info = _signed_monthly_edition_info()
    for path in _edition_paths():
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8-sig").strip()
                expires_at = ""
                license_type = ""
                activation_mode = ""
                duration_months = 0
                duration_days = 0
                issued_at = ""
                sponsor_id = ""
                license_id = ""
                if text.startswith("{"):
                    data = json.loads(text)
                    text = str(data.get("edition", "")).strip()
                    expires_at = str(data.get("expires_at", "") or "").strip()
                    license_type = str(data.get("license_type", "") or "").strip()
                    activation_mode = str(data.get("activation_mode", "") or "").strip()
                    duration_months = _safe_int(data.get("duration_months"), 0)
                    duration_days = _safe_int(data.get("duration_days"), 0)
                    issued_at = str(data.get("issued_at", "") or "").strip()
                    sponsor_id = str(data.get("sponsor_id", "") or "").strip()
                    license_id = str(data.get("license_id", "") or "").strip()
                normalized = _normalize_edition(text.lower())
                if normalized:
                    return _apply_expiry(
                        EditionInfo(
                            normalized,
                            expires_at=expires_at,
                            source=str(path),
                            license_type=license_type,
                            activation_mode=activation_mode,
                            duration_months=duration_months,
                            duration_days=duration_days,
                            issued_at=issued_at,
                            sponsor_id=sponsor_id,
                            license_id=license_id,
                        )
                    )
        except Exception:
            continue
    return signed_info or EditionInfo(TRIAL_EDITION)


def activate_monthly_license(code: str) -> TrialStatus:
    normalized = normalize_license_code(code)
    if not normalized:
        raise MonthlyLicenseError("请输入会员激活码")
    license_info = decode_monthly_license_code(normalized)
    today = _license_today()
    if today < license_info.valid_from:
        raise MonthlyLicenseError(f"该会员码将于 {license_info.valid_from.isoformat()} 开放激活")
    if license_info.is_expired(today):
        raise MonthlyLicenseError(f"该会员码的激活期限已于 {license_info.expires_at.isoformat()} 结束")

    with _STATE_LOCK:
        state = _load_state()
        records = _monthly_license_records(state)
        existing = _find_monthly_license_record(records, license_info.license_id)
        if existing is None:
            membership_start = max(today, license_info.valid_from)
            latest_expiry = _latest_membership_expiry(records)
            if latest_expiry is not None and latest_expiry > membership_start:
                membership_start = latest_expiry
            membership_expiry = _add_calendar_month(membership_start, license_info.duration_months)
            records.append(
                {
                    "code": normalized,
                    "period": license_info.period,
                    "license_id": license_info.license_id,
                    "activated_at": _now(),
                    "membership_start": membership_start.isoformat(),
                    "membership_expires_at": membership_expiry.isoformat(),
                }
            )
        else:
            membership_start, membership_expiry = _membership_bounds(existing, license_info)
            if membership_start is None or membership_expiry is None:
                raise MonthlyLicenseError("会员激活记录损坏")
            if today >= membership_expiry:
                raise MonthlyLicenseError(f"该会员码已使用，会员已于 {membership_expiry.isoformat()} 到期")
        records.sort(key=lambda item: str(item.get("period") or ""))
        state["monthly_license_codes"] = records[-12:]
        seen = _parse_date(str(state.get("license_max_seen_date") or ""))
        if seen is None or today > seen:
            state["license_max_seen_date"] = today.isoformat()
        state["counter"] = int(state.get("counter", 0) or 0) + 1
        state["updated_at"] = _now()
        _save_state(state)
    return trial_status()


def _signed_monthly_edition_info() -> EditionInfo | None:
    with _STATE_LOCK:
        state = _load_state()
        records = _monthly_license_records(state)
        if not records:
            return None

        today = _license_today()
        max_seen = _parse_date(str(state.get("license_max_seen_date") or ""))
        if max_seen is not None and today < max_seen:
            return EditionInfo(
                TRIAL_EDITION,
                source="signed-monthly-code",
                license_type=SIGNED_MONTHLY_LICENSE_TYPE,
                activation_mode=OFFLINE_MONTHLY_CODE,
                error=f"系统日期早于上次运行日期 {max_seen.isoformat()}，会员状态已暂停",
            )
        if max_seen is None or today > max_seen:
            state["license_max_seen_date"] = today.isoformat()
            state["updated_at"] = _now()
            _save_state(state)

    active: list[tuple[Any, dict[str, Any], date, date]] = []
    expired: list[tuple[Any, dict[str, Any], date, date]] = []
    future: list[tuple[Any, dict[str, Any], date, date]] = []
    valid_entries: list[tuple[Any, dict[str, Any], date, date]] = []
    invalid_errors: list[str] = []
    for record in records:
        try:
            license_info = decode_monthly_license_code(str(record.get("code") or ""))
        except MonthlyLicenseError as exc:
            invalid_errors.append(str(exc))
            continue
        membership_start, membership_expiry = _membership_bounds(record, license_info)
        if membership_start is None or membership_expiry is None:
            invalid_errors.append("会员激活记录损坏")
            continue
        row = (license_info, record, membership_start, membership_expiry)
        valid_entries.append(row)
        if membership_start <= today < membership_expiry:
            active.append(row)
        elif today >= membership_expiry:
            expired.append(row)
        else:
            future.append(row)

    if active:
        chain_expiry = max(row[3] for row in active)
        while True:
            extended = max(
                (row[3] for row in valid_entries if row[2] <= chain_expiry),
                default=chain_expiry,
            )
            if extended <= chain_expiry:
                break
            chain_expiry = extended
        license_info, record, _, _ = max(
            (row for row in valid_entries if row[2] <= chain_expiry),
            key=lambda row: row[3],
        )
        return EditionInfo(
            UNLIMITED_EDITION,
            expires_at=chain_expiry.isoformat(),
            activated_at=str(record.get("activated_at") or ""),
            source="signed-monthly-code",
            license_type=SIGNED_MONTHLY_LICENSE_TYPE,
            activation_mode=OFFLINE_MONTHLY_CODE,
            issued_at=license_info.issued_at,
            license_id=license_info.license_id,
        )
    if expired:
        license_info, record, _, membership_expiry = max(expired, key=lambda row: row[3])
        return EditionInfo(
            TRIAL_EDITION,
            expires_at=membership_expiry.isoformat(),
            activated_at=str(record.get("activated_at") or ""),
            expired=True,
            source="signed-monthly-code",
            license_type=SIGNED_MONTHLY_LICENSE_TYPE,
            activation_mode=OFFLINE_MONTHLY_CODE,
            issued_at=license_info.issued_at,
            license_id=license_info.license_id,
        )
    if invalid_errors and not future:
        return EditionInfo(
            TRIAL_EDITION,
            source="signed-monthly-code",
            license_type=SIGNED_MONTHLY_LICENSE_TYPE,
            activation_mode=OFFLINE_MONTHLY_CODE,
            error=invalid_errors[0],
        )
    if future:
        license_info, record, _, membership_expiry = max(future, key=lambda row: row[3])
        return EditionInfo(
            TRIAL_EDITION,
            expires_at=membership_expiry.isoformat(),
            activated_at=str(record.get("activated_at") or ""),
            source="signed-monthly-code",
            license_type=SIGNED_MONTHLY_LICENSE_TYPE,
            activation_mode=OFFLINE_MONTHLY_CODE,
            issued_at=license_info.issued_at,
            license_id=license_info.license_id,
        )
    return EditionInfo(TRIAL_EDITION)


def _monthly_license_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = [dict(item) for item in list(state.get("monthly_license_codes") or []) if isinstance(item, dict)]
    legacy_code = normalize_license_code(str(state.get("monthly_license_code") or ""))
    if legacy_code and not any(normalize_license_code(item.get("code", "")) == legacy_code for item in records):
        records.append(
            {
                "code": legacy_code,
                "activated_at": str(state.get("monthly_license_activated_at") or ""),
            }
        )
    return records


def _find_monthly_license_record(records: list[dict[str, Any]], license_id: str) -> dict[str, Any] | None:
    for record in records:
        record_id = str(record.get("license_id") or "").strip()
        if not record_id:
            try:
                record_id = decode_monthly_license_code(str(record.get("code") or "")).license_id
            except MonthlyLicenseError:
                continue
        if record_id == license_id:
            return record
    return None


def _latest_membership_expiry(records: list[dict[str, Any]]) -> date | None:
    expiries: list[date] = []
    for record in records:
        try:
            license_info = decode_monthly_license_code(str(record.get("code") or ""))
        except MonthlyLicenseError:
            continue
        _, membership_expiry = _membership_bounds(record, license_info)
        if membership_expiry is not None:
            expiries.append(membership_expiry)
    return max(expiries, default=None)


def _membership_bounds(record: dict[str, Any], license_info: Any) -> tuple[date | None, date | None]:
    membership_start = _parse_date(str(record.get("membership_start") or ""))
    membership_expiry = _parse_date(str(record.get("membership_expires_at") or ""))
    if membership_start is not None and membership_expiry is not None and membership_expiry > membership_start:
        return membership_start, membership_expiry

    activated_at = str(record.get("activated_at") or "").strip()
    activated_date = _parse_date(activated_at[:10])
    if activated_date is None:
        return None, None
    membership_start = max(activated_date, license_info.valid_from)
    return membership_start, _add_calendar_month(membership_start, license_info.duration_months)


def _add_calendar_month(value: date, months: int = 1) -> date:
    month_index = value.year * 12 + (value.month - 1) + max(1, int(months))
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _license_today() -> date:
    override = str(os.environ.get(LICENSE_TODAY_ENV) or "").strip()
    if override:
        parsed = _parse_date(override)
        if parsed is None:
            raise ValueError(f"{LICENSE_TODAY_ENV} must use YYYY-MM-DD")
        return parsed
    return date.today()


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _normalize_edition(value: str) -> str:
    text = str(value or "").strip().lower()
    if text == TRIAL_EDITION:
        return TRIAL_EDITION
    if text in UNLIMITED_EDITION_ALIASES:
        return UNLIMITED_EDITION
    return ""


def _apply_expiry(info: EditionInfo) -> EditionInfo:
    if info.edition != UNLIMITED_EDITION or not info.expires_at:
        if info.edition == UNLIMITED_EDITION and _requires_online_activation(info):
            return _apply_online_first_launch_expiry(info)
        return info
    if _requires_online_check(info):
        return _apply_online_fixed_expiry(info)
    expires_ts = _parse_expiry_timestamp(info.expires_at)
    if expires_ts is None:
        return EditionInfo(TRIAL_EDITION, expires_at=info.expires_at, expired=True, source=info.source)
    if time.time() > expires_ts:
        return EditionInfo(TRIAL_EDITION, expires_at=info.expires_at, expired=True, source=info.source)
    return info


def _requires_online_activation(info: EditionInfo) -> bool:
    return (
        info.license_type == MONTHLY_UNLIMITED_LICENSE
        and (info.activation_mode == ONLINE_FIRST_LAUNCH or info.duration_months > 0 or info.duration_days > 0)
    )


def _requires_online_check(info: EditionInfo) -> bool:
    return info.license_type == MONTHLY_UNLIMITED_LICENSE


def _apply_online_first_launch_expiry(info: EditionInfo) -> EditionInfo:
    try:
        now = _fetch_online_now()
    except Exception as exc:
        return EditionInfo(
            TRIAL_EDITION,
            source=info.source,
            license_type=info.license_type,
            activation_mode=info.activation_mode,
            online_required=True,
            error=f"月度无额度版需要联网激活：{exc}",
        )

    license_key = _license_key(info)
    with _STATE_LOCK:
        state = _load_state()
        activations = dict(state.get("license_activations") or {})
        activation = dict(activations.get(license_key) or {})
        activated_at = str(activation.get("activated_at") or "").strip()
        expires_at = str(activation.get("expires_at") or "").strip()
        if not activated_at or not expires_at:
            activated_at = _iso_utc(now)
            expires_at = _calculate_online_license_expiry(now, info)
        activation.update(
            {
                "license_type": info.license_type,
                "activation_mode": info.activation_mode or ONLINE_FIRST_LAUNCH,
                "activated_at": activated_at,
                "expires_at": expires_at,
                "checked_at": _iso_utc(now),
                "source": info.source,
            }
        )
        activations[license_key] = activation
        state["license_activations"] = activations
        state["updated_at"] = _now()
        _save_state(state)

    expires_dt = _parse_expiry_datetime(expires_at)
    if expires_dt is None or now > expires_dt.astimezone(timezone.utc):
        return EditionInfo(
            TRIAL_EDITION,
            expires_at=expires_at,
            activated_at=activated_at,
            expired=True,
            source=info.source,
            license_type=info.license_type,
            activation_mode=info.activation_mode,
        )
    return EditionInfo(
        UNLIMITED_EDITION,
        expires_at=expires_at,
        activated_at=activated_at,
        source=info.source,
        license_type=info.license_type,
        activation_mode=info.activation_mode,
    )


def _apply_online_fixed_expiry(info: EditionInfo) -> EditionInfo:
    try:
        now = _fetch_online_now()
    except Exception as exc:
        return EditionInfo(
            TRIAL_EDITION,
            expires_at=info.expires_at,
            source=info.source,
            license_type=info.license_type,
            activation_mode=info.activation_mode,
            online_required=True,
            error=f"月度无额度版需要联网校验有效期：{exc}",
        )
    expires_dt = _parse_expiry_datetime(info.expires_at)
    if expires_dt is None or now > expires_dt.astimezone(timezone.utc):
        return EditionInfo(
            TRIAL_EDITION,
            expires_at=info.expires_at,
            expired=True,
            source=info.source,
            license_type=info.license_type,
            activation_mode=info.activation_mode,
        )
    return info


def _calculate_online_license_expiry(now: datetime, info: EditionInfo) -> str:
    local_today = now.astimezone().date()
    if info.duration_days > 0:
        return (local_today + timedelta(days=info.duration_days)).isoformat()
    months = info.duration_months if info.duration_months > 0 else 1
    return _add_months(local_today, months).isoformat()


def _add_months(start: date, months: int) -> date:
    month = start.month - 1 + max(1, months)
    year = start.year + month // 12
    month = month % 12 + 1
    month_lengths = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    return date(year, month, min(start.day, month_lengths[month - 1]))


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _license_key(info: EditionInfo) -> str:
    raw = "|".join(
        [
            info.license_id,
            info.license_type,
            info.activation_mode,
            str(info.duration_months),
            str(info.duration_days),
            info.issued_at,
            info.sponsor_id,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _fetch_online_now() -> datetime:
    override = os.environ.get("ENGAIXT_LICENSE_NOW_UTC")
    if override:
        parsed = datetime.fromisoformat(override.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    urls = [
        item.strip()
        for item in os.environ.get("ENGAIXT_LICENSE_TIME_URLS", "").split(";")
        if item.strip()
    ] or _ONLINE_TIME_URLS
    errors: list[str] = []
    for url in urls:
        try:
            return _fetch_online_now_from_url(url)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors) or "没有可用的在线时间源")


def _fetch_online_now_from_url(url: str) -> datetime:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "EngAixt/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            date_header = resp.headers.get("Date")
    except Exception:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "EngAixt/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            date_header = resp.headers.get("Date")
    if not date_header:
        raise RuntimeError("响应缺少 Date 头")
    parsed = email.utils.parsedate_to_datetime(date_header)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _parse_expiry_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime.combine(parsed_date, datetime_time.max).astimezone()
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    except Exception:
        return None


def _parse_expiry_timestamp(value: str) -> float | None:
    parsed = _parse_expiry_datetime(value)
    if parsed is None:
        return None
    return parsed.timestamp()


def is_trial_edition() -> bool:
    return current_edition() != UNLIMITED_EDITION


def trial_status() -> TrialStatus:
    edition_info = current_edition_info()
    edition = edition_info.edition
    with _STATE_LOCK:
        state = _load_state()
    used = float(state.get("quota_cny_used", 0.0)) if edition == TRIAL_EDITION else 0.0
    remaining = max(0.0, TRIAL_QUOTA_CNY - used) if edition == TRIAL_EDITION else float("inf")
    quota_period = str(state.get("quota_period") or _current_trial_period())
    quota_resets_at = str(state.get("quota_resets_at") or _next_trial_period_start(quota_period))
    return TrialStatus(
        edition=edition,
        quota_cny_total=TRIAL_QUOTA_CNY if edition == TRIAL_EDITION else 0.0,
        quota_cny_used=round(used, 6),
        quota_cny_remaining=round(remaining, 6) if remaining != float("inf") else remaining,
        quota_exceeded=edition == TRIAL_EDITION and remaining <= 0,
        machine_id=_machine_id(),
        state_paths=[str(path) for path in _state_paths()],
        quota_period=quota_period,
        quota_resets_at=quota_resets_at,
        quota_reset_policy=str(state.get("quota_reset_policy") or TRIAL_QUOTA_RESET_POLICY),
        license_expires_at=edition_info.expires_at,
        license_activated_at=edition_info.activated_at,
        license_expired=edition_info.expired,
        license_source=edition_info.source,
        license_online_required=edition_info.online_required,
        license_error=edition_info.error,
    )


def ensure_translation_quota_available() -> None:
    status = trial_status()
    if status.license_online_required:
        raise TrialQuotaExceeded(status.license_error or "月度无额度版需要联网激活或校验。")
    if status.edition == TRIAL_EDITION and status.quota_cny_remaining <= 0:
        if status.license_expired:
            raise TrialQuotaExceeded(
                f"无额度版已于 {status.license_expires_at or '未知日期'} 到期，且 20 元试用额度已用完，请获取新的月度无额度版。"
            )
        raise TrialQuotaExceeded(
            f"20 元试用额度已用完，请使用无额度版继续翻译。"
        )


def charge_translation_cost(cost_cny: float, *, source: str = "api", details: dict[str, Any] | None = None) -> TrialStatus:
    if not is_trial_edition():
        return trial_status()
    cost = max(0.0, float(cost_cny or 0.0))
    if cost <= 0:
        return trial_status()
    with _STATE_LOCK:
        state = _load_state()
        state["quota_cny_used"] = round(float(state.get("quota_cny_used", 0.0)) + cost, 6)
        state["counter"] = int(state.get("counter", 0)) + 1
        state["updated_at"] = _now()
        tasks = list(state.get("tasks") or [])
        task = {
            "at": state["updated_at"],
            "quota_period": state.get("quota_period") or _current_trial_period(),
            "source": source,
            "cost_cny": round(cost, 6),
        }
        if details:
            task["details"] = details
        tasks.append(task)
        state["tasks"] = tasks[-200:]
        _save_state(state)
    return trial_status()


def _edition_paths() -> list[Path]:
    root = app_root()
    paths = [
        root / "edition.txt",
        root / "edition.json",
        Path.cwd() / "edition.txt",
        Path.cwd() / "edition.json",
    ]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        paths.extend([
            exe_dir / "edition.txt",
            exe_dir / "edition.json",
        ])
    return paths


def _state_paths() -> list[Path]:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return [Path(override) / _STATE_FILE]
    paths: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / _APP_NAME / _STATE_FILE)
    programdata = os.environ.get("PROGRAMDATA")
    if programdata:
        paths.append(Path(programdata) / _APP_NAME / _STATE_FILE)
    paths.append(Path.home() / f".{_APP_NAME.lower()}" / _STATE_FILE)
    return paths


def _load_state() -> dict[str, Any]:
    states = []
    for path in _state_paths():
        state = _read_state(path)
        if state:
            states.append(state)
    if states:
        state = max(states, key=lambda s: int(s.get("counter", 0)))
    else:
        state = _new_state()
    state = _apply_trial_period(state)
    _save_state(state)
    return state


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if data.get("machine_id") != _machine_id():
        return None
    checksum = str(data.get("checksum", ""))
    payload = dict(data)
    payload.pop("checksum", None)
    if checksum != _checksum(payload):
        return None
    return payload


def _save_state(state: dict[str, Any]) -> None:
    payload = dict(state)
    payload["schema_version"] = 1
    payload["machine_id"] = _machine_id()
    payload.setdefault("created_at", _now())
    payload.setdefault("quota_cny_total", TRIAL_QUOTA_CNY)
    payload.setdefault("quota_cny_used", 0.0)
    payload.setdefault("quota_period", _current_trial_period())
    payload.setdefault("quota_period_started_at", f"{payload['quota_period']}-01")
    payload["quota_reset_policy"] = TRIAL_QUOTA_RESET_POLICY
    payload["quota_resets_at"] = _next_trial_period_start(str(payload.get("quota_period") or ""))
    payload.setdefault("counter", 0)
    payload["checksum"] = _checksum({k: v for k, v in payload.items() if k != "checksum"})
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    for path in _state_paths():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(encoded, encoding="utf-8")
            tmp.replace(path)
        except Exception:
            continue


def _new_state() -> dict[str, Any]:
    now = _now()
    period = _current_trial_period()
    return {
        "schema_version": 1,
        "machine_id": _machine_id(),
        "created_at": now,
        "updated_at": now,
        "quota_cny_total": TRIAL_QUOTA_CNY,
        "quota_cny_used": 0.0,
        "quota_period": period,
        "quota_period_started_at": f"{period}-01",
        "quota_resets_at": _next_trial_period_start(period),
        "quota_reset_policy": TRIAL_QUOTA_RESET_POLICY,
        "counter": 0,
        "tasks": [],
    }


def _apply_trial_period(state: dict[str, Any]) -> dict[str, Any]:
    current_period = _current_trial_period()
    stored_period = _state_trial_period(state)
    normalized = dict(state)
    normalized["quota_reset_policy"] = TRIAL_QUOTA_RESET_POLICY
    if stored_period == current_period:
        normalized["quota_period"] = current_period
        normalized.setdefault("quota_period_started_at", f"{current_period}-01")
        normalized["quota_resets_at"] = _next_trial_period_start(current_period)
        return normalized

    history = list(normalized.get("quota_reset_history") or [])
    history.append(
        {
            "period": stored_period,
            "quota_cny_used": round(float(normalized.get("quota_cny_used", 0.0) or 0.0), 6),
            "reset_at": _now(),
            "tasks_count": len(list(normalized.get("tasks") or [])),
        }
    )
    normalized["quota_reset_history"] = history[-24:]
    normalized["quota_cny_used"] = 0.0
    normalized["quota_period"] = current_period
    normalized["quota_period_started_at"] = f"{current_period}-01"
    normalized["quota_resets_at"] = _next_trial_period_start(current_period)
    normalized["tasks"] = []
    normalized["counter"] = int(normalized.get("counter", 0) or 0) + 1
    normalized["updated_at"] = _now()
    return normalized


def _state_trial_period(state: dict[str, Any]) -> str:
    period = str(state.get("quota_period") or "").strip()
    if _is_trial_period(period):
        return period
    for key in ("updated_at", "created_at"):
        period = _period_from_timestamp(str(state.get(key) or ""))
        if period:
            return period
    return _current_trial_period()


def _current_trial_period() -> str:
    return time.strftime("%Y-%m", time.localtime())


def _next_trial_period_start(period: str | None = None) -> str:
    text = str(period or _current_trial_period()).strip()
    if not _is_trial_period(text):
        text = _current_trial_period()
    year, month = [int(part) for part in text.split("-", 1)]
    if month == 12:
        year += 1
        month = 1
    else:
        month += 1
    return f"{year:04d}-{month:02d}-01"


def _is_trial_period(value: str) -> bool:
    try:
        year, month = [int(part) for part in str(value).split("-", 1)]
    except Exception:
        return False
    return 1 <= month <= 12 and year >= 2000


def _period_from_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 7 and _is_trial_period(text[:7]):
        return text[:7]
    return ""


def _checksum(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((raw + "|" + _CHECKSUM_SALT).encode("utf-8")).hexdigest()


def _machine_id() -> str:
    parts = [
        platform.node(),
        platform.platform(),
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERDOMAIN", ""),
        str(uuid.getnode()),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()[:32]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
