from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from urllib.parse import urlencode

from curl_cffi import requests

from moviemax.config import Settings
from moviemax.models import Screening


class CgvError(RuntimeError):
    """Raised when CGV data cannot be retrieved or validated."""


_FORMAT_DISCRIMINATOR_FIELDS = (
    "movkndCd",
    "movkndDsplEnm",
    "movkndDsplNm",
    "scnsNm",
    "expoScnsNm",
    "tcscnsGradNm",
    "scnsGradCd",
)


def _non_negative_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CgvError(f"CGV returned an invalid {field}") from exc
    if result < 0:
        raise CgvError(f"CGV returned a negative {field}")
    return result


def _validated_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise CgvError(f"CGV returned an invalid {field}")
    try:
        date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
    except ValueError as exc:
        raise CgvError(f"CGV returned an invalid {field}") from exc
    return value


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CgvError(f"CGV returned an invalid {field}")
    return value.strip()


def _validated_site_no(value: Any) -> str:
    return _required_string(value, "siteNo")


def _validate_format_discriminators(row: Mapping[str, Any], index: int) -> None:
    if not any(
        str(row.get(field) or "").strip() for field in _FORMAT_DISCRIMINATOR_FIELDS
    ):
        raise CgvError(
            f"CGV schedule row {index} is missing all format discriminator fields"
        )


def _format_name(row: Mapping[str, Any], index: int) -> str:
    for field in ("movkndDsplEnm", "movkndDsplNm", "tcscnsGradNm"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    raise CgvError(f"CGV schedule row {index} is missing its format name")


def _format_code(row: Mapping[str, Any], index: int) -> str:
    return _required_string(row.get("movkndCd"), f"schedule row {index} movkndCd")


class CgvClient:
    def __init__(self, settings: Settings, session: Any | None = None) -> None:
        self.settings = settings
        self.session = session or requests.Session(
            impersonate=settings.cgv_impersonate,
            headers={
                "Accept": "application/json",
                "Accept-Language": "ko-KR",
                "Referer": f"{settings.cgv_base_url}/cnm/movieBook/movie",
            },
        )

    def _get_list(self, path: str, params: Mapping[str, str]) -> list[dict[str, Any]]:
        url = f"{self.settings.cgv_base_url}{path}"
        try:
            response = self.session.get(
                url,
                params=dict(params),
                timeout=self.settings.request_timeout_seconds,
            )
        except Exception as exc:  # curl-cffi exposes multiple transport exception types
            raise CgvError(f"CGV request failed ({type(exc).__name__})") from exc

        if int(response.status_code) != 200:
            hint = (
                " Cloudflare or CGV may be blocking this server."
                if response.status_code in {403, 429}
                else ""
            )
            raise CgvError(f"CGV returned HTTP {response.status_code}.{hint}")
        try:
            payload = response.json()
        except Exception as exc:
            raise CgvError("CGV returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise CgvError("CGV returned an invalid response object")
        try:
            status_code = int(payload.get("statusCode", -1))
        except (TypeError, ValueError) as exc:
            raise CgvError("CGV returned an invalid status code") from exc
        if status_code != 0:
            message = str(payload.get("statusMessage") or "unknown CGV error")
            raise CgvError(f"CGV API error: {message}")
        data = payload.get("data")
        if data is None:
            return []
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
            raise CgvError("CGV response data is not a list")
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise CgvError(f"CGV response data row {index} is not an object")
            rows.append(dict(item))
        return rows

    def resolve_movie_no(self) -> str:
        if self.settings.movie_no:
            return self.settings.movie_no
        movies = self._get_list(
            "/api/v1/booking/searchAtktTopPostrList",
            {
                "coCd": self.settings.company_code,
                "movNm": "",
                "div": "",
                "attrCd": "",
            },
        )
        matches = [
            m
            for m in movies
            if str(m.get("movNm", "")).strip() == self.settings.movie_name
        ]
        if len(matches) != 1 or not matches[0].get("movNo"):
            raise CgvError(
                f"Could not uniquely resolve movie number for {self.settings.movie_name}"
            )
        return str(matches[0]["movNo"])

    def get_regions_and_sites(self) -> list[dict[str, Any]]:
        rows = self._get_list(
            "/api/v1/booking/searchRegnList",
            {"coCd": self.settings.company_code},
        )
        regions: list[dict[str, Any]] = []
        for region_index, row in enumerate(rows):
            region_code = _required_string(
                row.get("regnGrpCd"),
                f"region row {region_index} regnGrpCd",
            )
            region_name = _required_string(
                row.get("regnGrpNm"),
                f"region row {region_index} regnGrpNm",
            )
            raw_sites = row.get("siteList")
            if not isinstance(raw_sites, Sequence) or isinstance(
                raw_sites, (str, bytes, bytearray)
            ):
                raise CgvError(f"CGV region row {region_index} siteList is not a list")

            sites: list[dict[str, str]] = []
            for site_index, raw_site in enumerate(raw_sites):
                if not isinstance(raw_site, Mapping):
                    raise CgvError(
                        "CGV region row "
                        f"{region_index} site row {site_index} is not an object"
                    )
                sites.append(
                    {
                        "site_no": _validated_site_no(raw_site.get("siteNo")),
                        "site_name": _required_string(
                            raw_site.get("siteNm"),
                            f"site row {site_index} siteNm",
                        ),
                        "operation_status": _required_string(
                            raw_site.get("bzplcOperStusNm"),
                            f"site row {site_index} bzplcOperStusNm",
                        ),
                    }
                )
            regions.append(
                {
                    "region_code": region_code,
                    "region_name": region_name,
                    "sites": sites,
                }
            )
        return regions

    def get_site_screening_dates(self, site_no: str) -> list[str]:
        validated_site_no = _validated_site_no(site_no)
        rows = self._get_list(
            "/api/v1/booking/searchSiteScnscYmdListBySite",
            {
                "coCd": self.settings.company_code,
                "siteNo": validated_site_no,
            },
        )
        dates: set[str] = set()
        for index, row in enumerate(rows):
            try:
                value = _validated_date(row.get("scnYmd"), "scnYmd")
            except CgvError as exc:
                raise CgvError(
                    f"CGV site date row {index} has an invalid scnYmd"
                ) from exc
            dates.add(value)
        return sorted(dates)

    def get_site_movies(self, site_no: str) -> list[dict[str, Any]]:
        validated_site_no = _validated_site_no(site_no)
        screening_dates = self.get_site_screening_dates(validated_site_no)
        movies: dict[str, dict[str, Any]] = {}

        for date_index, screening_date in enumerate(screening_dates):
            if date_index and self.settings.request_gap_seconds:
                time.sleep(self.settings.request_gap_seconds)
            rows = self._get_list(
                "/api/v1/booking/searchMovScnInfo",
                {
                    "coCd": self.settings.company_code,
                    "siteNo": validated_site_no,
                    "scnYmd": screening_date,
                    "rtctlScopCd": "08",
                },
            )
            for row_index, row in enumerate(rows):
                _validate_format_discriminators(row, row_index)
                movie_no = _required_string(
                    row.get("movNo"),
                    f"schedule row {row_index} movNo",
                )
                movie_name = _required_string(
                    row.get("movNm"),
                    f"schedule row {row_index} movNm",
                )
                format_code = _format_code(row, row_index)
                format_name = _format_name(row, row_index)
                grade_name = str(row.get("tcscnsGradNm") or "").strip()
                screen_name = str(
                    row.get("expoScnsNm") or row.get("scnsNm") or ""
                ).strip()
                entry = movies.setdefault(
                    movie_no,
                    {
                        "movie_no": movie_no,
                        "movie_name": movie_name,
                        "formats": {},
                        "screening_dates": set(),
                    },
                )
                format_entry = entry["formats"].setdefault(
                    format_code,
                    {
                        "format_code": format_code,
                        "format_names": set(),
                        "screen_grade_names": set(),
                        "screen_names": set(),
                        "screening_dates": set(),
                    },
                )
                format_entry["format_names"].add(format_name)
                if grade_name:
                    format_entry["screen_grade_names"].add(grade_name)
                if screen_name:
                    format_entry["screen_names"].add(screen_name)
                format_entry["screening_dates"].add(screening_date)
                entry["screening_dates"].add(screening_date)

        return sorted(
            (
                {
                    "movie_no": movie_no,
                    "movie_name": str(entry["movie_name"]),
                    "formats": sorted(
                        (
                            {
                                "format_code": str(format_code),
                                "format_name": min(
                                    format_entry["format_names"],
                                    key=lambda value: (-len(value), value.casefold()),
                                ),
                                "screen_grade_names": sorted(
                                    format_entry["screen_grade_names"]
                                ),
                                "screen_names": sorted(format_entry["screen_names"]),
                                "screening_dates": sorted(
                                    format_entry["screening_dates"]
                                ),
                            }
                            for format_code, format_entry in entry["formats"].items()
                        ),
                        key=lambda item: (item["format_name"], item["format_code"]),
                    ),
                    "screening_dates": sorted(entry["screening_dates"]),
                }
                for movie_no, entry in movies.items()
            ),
            key=lambda item: (item["movie_name"], item["movie_no"]),
        )

    def get_screening_dates(self, movie_no: str) -> list[str]:
        site_no = _validated_site_no(self.settings.site_no)
        rows = self._get_list(
            "/api/v1/booking/searchSiteScnscYmdListByMov",
            {
                "coCd": self.settings.company_code,
                "siteNo": site_no,
                "movNo": movie_no,
            },
        )
        dates: set[str] = set()
        for index, row in enumerate(rows):
            try:
                value = _validated_date(row.get("scnYmd"), "scnYmd")
            except CgvError as exc:
                raise CgvError(f"CGV date row {index} has an invalid scnYmd") from exc
            dates.add(value)
        return sorted(dates)

    def get_screenings(self, movie_no: str, screening_date: str) -> list[Screening]:
        rows = self.get_screening_rows(movie_no, screening_date)
        return self.parse_screening_rows(movie_no, screening_date, rows)

    def get_screening_rows(
        self,
        movie_no: str,
        screening_date: str,
    ) -> list[dict[str, Any]]:
        site_no = _validated_site_no(self.settings.site_no)
        return self._get_list(
            "/api/v1/booking/searchSchByMov",
            {
                "coCd": self.settings.company_code,
                "siteNo": site_no,
                "scnYmd": screening_date,
                "movNo": movie_no,
                "rtctlScopCd": "08",
            },
        )

    def parse_screening_rows(
        self,
        movie_no: str,
        screening_date: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> list[Screening]:
        screenings: list[Screening] = []
        for index, row in enumerate(rows):
            _validate_format_discriminators(row, index)
            if self.settings.format_code and not str(row.get("movkndCd") or "").strip():
                raise CgvError(
                    f"CGV schedule row {index} is missing its movkndCd format code"
                )
            if not self._is_target_format(row):
                continue
            screenings.append(
                self._parse_screening(row, movie_no, screening_date, index=index)
            )
        return sorted(
            screenings,
            key=lambda item: (
                item.screening_date,
                item.start_time,
                item.screen_no,
                item.sequence,
            ),
        )

    def _is_target_format(self, row: Mapping[str, Any]) -> bool:
        format_code = self.settings.format_code.strip()
        if format_code:
            return str(row.get("movkndCd") or "").strip() == format_code

        keyword = self.settings.format_keyword.upper()
        text = " ".join(
            str(row.get(field) or "")
            for field in _FORMAT_DISCRIMINATOR_FIELDS
            if field != "scnsGradCd"
        ).upper()
        keyword_match = bool(keyword and keyword in text)
        grade_match = bool(
            self.settings.screen_grade_code
            and str(row.get("scnsGradCd") or "") == self.settings.screen_grade_code
        )
        return keyword_match or grade_match

    def _parse_screening(
        self,
        row: Mapping[str, Any],
        movie_no: str,
        screening_date: str,
        *,
        index: int,
    ) -> Screening:
        screen_no = str(row.get("scnsNo") or "").strip()
        sequence = str(row.get("scnSseq") or "").strip()
        start_time = str(row.get("scnsrtTm") or "").strip()
        if (
            not screen_no
            or not sequence
            or len(start_time) != 4
            or not start_time.isdigit()
        ):
            raise CgvError("CGV schedule is missing its stable screening identifiers")

        actual_date = _validated_date(
            row.get("scnYmd") or screening_date,
            "screening date",
        )
        params = urlencode(
            {
                "scnYmd": actual_date,
                "siteNo": self.settings.site_no,
                "siteNm": self.settings.site_name,
                "movNo": movie_no,
                "scnSseq": sequence,
                "scnsNo": screen_no,
            }
        )
        control_value = next(
            (
                row[field]
                for field in ("cntlYn", "atktCntlYn", "rtktCntlYn")
                if row.get(field) is not None
            ),
            None,
        )
        control_yn = str(control_value or "").strip().upper()
        if control_yn not in {"Y", "N"}:
            raise CgvError("CGV schedule has an invalid booking control flag")
        free_seat_value = row.get("frSeatCnt")
        if free_seat_value is None and control_yn == "Y":
            free_seat_value = 0
        return Screening(
            company_code=str(row.get("coCd") or self.settings.company_code),
            site_no=str(row.get("siteNo") or self.settings.site_no),
            site_name=self.settings.site_name,
            movie_no=str(row.get("movNo") or movie_no),
            movie_name=str(row.get("movNm") or self.settings.movie_name),
            screening_date=actual_date,
            screen_no=screen_no,
            screen_name=str(row.get("expoScnsNm") or row.get("scnsNm") or "상영관"),
            sequence=sequence,
            start_time=start_time,
            end_time=str(row.get("scnendTm") or "").strip(),
            format_name=_format_name(row, index),
            screen_grade_code=str(row.get("scnsGradCd") or ""),
            total_seats=_non_negative_int(row.get("stcnt"), "total seat count"),
            free_seats=_non_negative_int(free_seat_value, "free seat count"),
            control_yn=control_yn,
            booking_url=f"{self.settings.cgv_base_url}/cnm/movieBook/movie?{params}",
        )
