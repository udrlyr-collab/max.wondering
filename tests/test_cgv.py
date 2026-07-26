from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from moviemax.cgv import CgvClient, CgvError
from moviemax.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, dict(kwargs)))
        name = (
            "cgv_dates.json"
            if "searchSiteScnscYmdListByMov" in url
            else "cgv_schedule.json"
        )
        return FakeResponse(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


class StaticSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get(self, _url: str, **_kwargs: object) -> FakeResponse:
        return FakeResponse(self.payload)


class RoutingSession:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        call = dict(kwargs)
        self.calls.append((url, call))
        for marker, configured in self.routes.items():
            if marker not in url:
                continue
            params = dict(call.get("params") or {})
            payload = configured(params) if callable(configured) else configured
            assert isinstance(payload, dict)
            return FakeResponse(payload)
        raise AssertionError(f"Unexpected CGV URL: {url}")


def test_parses_dates_and_filters_legacy_imax_target() -> None:
    session = FakeSession()
    client = CgvClient(Settings(), session=session)

    dates = client.get_screening_dates("30001323")
    screenings = client.get_screenings("30001323", dates[0])

    assert dates == ["20260805", "20260806"]
    assert len(screenings) == 1
    screening = screenings[0]
    assert screening.screen_name == "IMAX관"
    assert screening.free_seats == 2
    assert screening.total_seats == 624
    assert screening.key.endswith("|018|2")
    booking_query = parse_qs(urlparse(screening.booking_url).query)
    assert booking_query == {
        "movNo": ["30001323"],
        "scnSseq": ["2"],
        "scnYmd": ["20260805"],
        "scnsNo": ["018"],
        "siteNm": ["용산아이파크몰"],
        "siteNo": ["0013"],
    }


def test_reports_cloudflare_block_without_parsing_html() -> None:
    class BlockedResponse:
        status_code = 403

    class BlockedSession:
        def get(self, _url: str, **_kwargs: object) -> BlockedResponse:
            return BlockedResponse()

    client = CgvClient(Settings(), session=BlockedSession())
    with pytest.raises(CgvError, match="blocking this server"):
        client.get_screening_dates("30001323")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"statusCode": 0, "data": ["not-an-object"]}, "row 0 is not an object"),
        ({"statusCode": 0, "data": [{"scnYmd": "20260230"}]}, "invalid scnYmd"),
    ],
)
def test_rejects_malformed_date_responses(payload: dict, error: str) -> None:
    client = CgvClient(Settings(), session=StaticSession(payload))

    with pytest.raises(CgvError, match=error):
        client.get_screening_dates("30001323")


def test_rejects_schedule_without_format_discriminator() -> None:
    payload = {
        "statusCode": 0,
        "data": [
            {
                "scnYmd": "20260805",
                "scnsNo": "018",
                "scnSseq": "2",
                "scnsrtTm": "1000",
                "stcnt": "624",
                "frSeatCnt": "2",
                "cntlYn": "N",
            }
        ],
    }
    client = CgvClient(Settings(), session=StaticSession(payload))

    with pytest.raises(CgvError, match="format discriminator"):
        client.get_screenings("30001323", "20260805")


def test_rejects_target_schedule_without_booking_control_flag() -> None:
    payload = json.loads((FIXTURES / "cgv_schedule.json").read_text(encoding="utf-8"))
    del payload["data"][0]["cntlYn"]
    client = CgvClient(Settings(), session=StaticSession(payload))

    with pytest.raises(CgvError, match="booking control flag"):
        client.get_screenings("30001323", "20260805")


def test_get_regions_and_sites_returns_json_catalog_with_operation_status() -> None:
    payload = {
        "statusCode": 0,
        "data": [
            {
                "regnGrpCd": "01",
                "regnGrpNm": "Seoul",
                "siteList": [
                    {
                        "siteNo": "0013",
                        "siteNm": "Yongsan I-Park Mall",
                        "bzplcOperStusNm": "open",
                    },
                    {
                        "siteNo": "P001",
                        "siteNm": "Cine de Chef",
                        "bzplcOperStusNm": "renovating",
                    },
                ],
            }
        ],
    }
    client = CgvClient(Settings(), session=StaticSession(payload))

    catalog = client.get_regions_and_sites()

    assert catalog == [
        {
            "region_code": "01",
            "region_name": "Seoul",
            "sites": [
                {
                    "site_no": "0013",
                    "site_name": "Yongsan I-Park Mall",
                    "operation_status": "open",
                },
                {
                    "site_no": "P001",
                    "site_name": "Cine de Chef",
                    "operation_status": "renovating",
                },
            ],
        }
    ]
    json.dumps(catalog)


def test_get_site_movies_walks_all_dates_and_groups_all_formats_by_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates_payload = {
        "statusCode": 0,
        "data": [{"scnYmd": "20260805"}, {"scnYmd": "20260806"}],
    }

    def schedule_payload(params: dict) -> dict:
        screening_date = params["scnYmd"]
        imax_format = "IMAX LASER 2D" if screening_date == "20260805" else "IMAX 2D"
        return {
            "statusCode": 0,
            "data": [
                {
                    "movNo": "30001323",
                    "movNm": "Odyssey",
                    "movkndCd": "48",
                    "movkndDsplEnm": imax_format,
                    "tcscnsGradNm": "아이맥스",
                    "scnsNm": "IMAX관",
                    "scnsGradCd": "0301",
                },
                {
                    "movNo": "30000001",
                    "movNm": "Normal Movie",
                    "movkndCd": "02",
                    "movkndDsplEnm": "2D",
                    "tcscnsGradNm": "일반",
                    "scnsNm": "1관",
                    "scnsGradCd": "0101",
                },
            ],
        }

    session = RoutingSession(
        {
            "searchSiteScnscYmdListBySite": dates_payload,
            "searchMovScnInfo": schedule_payload,
        }
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr("moviemax.cgv.time.sleep", sleep_calls.append)
    client = CgvClient(Settings(request_gap_seconds=0.25), session=session)

    catalog = client.get_site_movies("0013")

    assert catalog == [
        {
            "movie_no": "30000001",
            "movie_name": "Normal Movie",
            "formats": [
                {
                    "format_code": "02",
                    "format_name": "2D",
                    "screen_grade_names": ["일반"],
                    "screen_names": ["1관"],
                    "screening_dates": ["20260805", "20260806"],
                }
            ],
            "screening_dates": ["20260805", "20260806"],
        },
        {
            "movie_no": "30001323",
            "movie_name": "Odyssey",
            "formats": [
                {
                    "format_code": "48",
                    "format_name": "IMAX LASER 2D",
                    "screen_grade_names": ["아이맥스"],
                    "screen_names": ["IMAX관"],
                    "screening_dates": ["20260805", "20260806"],
                }
            ],
            "screening_dates": ["20260805", "20260806"],
        },
    ]
    json.dumps(catalog)
    schedule_calls = [call for url, call in session.calls if "searchMovScnInfo" in url]
    assert [call["params"]["scnYmd"] for call in schedule_calls] == [
        "20260805",
        "20260806",
    ]
    assert all(call["params"]["siteNo"] == "0013" for call in schedule_calls)
    assert all(call["params"]["rtctlScopCd"] == "08" for call in schedule_calls)
    assert sleep_calls == [0.25]


def test_get_site_movies_includes_normal_schedule_rows() -> None:
    session = RoutingSession(
        {
            "searchSiteScnscYmdListBySite": {
                "statusCode": 0,
                "data": [{"scnYmd": "20260805"}],
            },
            "searchMovScnInfo": {
                "statusCode": 0,
                "data": [
                    {
                        "movNo": "30000001",
                        "movNm": "Normal Movie",
                        "movkndCd": "02",
                        "movkndDsplEnm": "2D",
                        "tcscnsGradNm": "일반",
                        "scnsNm": "1관",
                        "scnsGradCd": "0101",
                    }
                ],
            },
        }
    )
    client = CgvClient(Settings(request_gap_seconds=0), session=session)

    assert client.get_site_movies("0013") == [
        {
            "movie_no": "30000001",
            "movie_name": "Normal Movie",
            "formats": [
                {
                    "format_code": "02",
                    "format_name": "2D",
                    "screen_grade_names": ["일반"],
                    "screen_names": ["1관"],
                    "screening_dates": ["20260805"],
                }
            ],
            "screening_dates": ["20260805"],
        }
    ]


@pytest.mark.parametrize("site_no", [13, "", "   "])
def test_site_catalog_rejects_non_string_or_empty_site_no(site_no: object) -> None:
    client = CgvClient(Settings(), session=StaticSession({"statusCode": 0, "data": []}))

    with pytest.raises(CgvError, match="invalid siteNo"):
        client.get_site_screening_dates(site_no)  # type: ignore[arg-type]


def test_region_catalog_rejects_non_string_site_no() -> None:
    payload = {
        "statusCode": 0,
        "data": [
            {
                "regnGrpCd": "01",
                "regnGrpNm": "Seoul",
                "siteList": [
                    {
                        "siteNo": 13,
                        "siteNm": "Malformed Site",
                        "bzplcOperStusNm": "open",
                    }
                ],
            }
        ],
    }
    client = CgvClient(Settings(), session=StaticSession(payload))

    with pytest.raises(CgvError, match="invalid siteNo"):
        client.get_regions_and_sites()


def test_site_movie_catalog_rejects_missing_format_discriminators() -> None:
    session = RoutingSession(
        {
            "searchSiteScnscYmdListBySite": {
                "statusCode": 0,
                "data": [{"scnYmd": "20260805"}],
            },
            "searchMovScnInfo": {
                "statusCode": 0,
                "data": [{"movNo": "30001323", "movNm": "Odyssey"}],
            },
        }
    )
    client = CgvClient(Settings(request_gap_seconds=0), session=session)

    with pytest.raises(CgvError, match="format discriminator"):
        client.get_site_movies("0013")


def test_format_code_exactly_separates_normal_2d_from_special_formats() -> None:
    payload = json.loads((FIXTURES / "cgv_schedule.json").read_text(encoding="utf-8"))
    client = CgvClient(
        Settings(format_code="02", format_keyword="2D", screen_grade_code=""),
        session=StaticSession(payload),
    )

    screenings = client.get_screenings("30001323", "20260805")

    assert len(screenings) == 1
    assert screenings[0].format_name == "2D"
    assert screenings[0].screen_name == "1관"


def test_exact_format_target_rejects_rows_without_format_code() -> None:
    payload = json.loads((FIXTURES / "cgv_schedule.json").read_text(encoding="utf-8"))
    del payload["data"][1]["movkndCd"]
    client = CgvClient(
        Settings(format_code="02", format_keyword="2D", screen_grade_code=""),
        session=StaticSession(payload),
    )

    with pytest.raises(CgvError, match="movkndCd format code"):
        client.get_screenings("30001323", "20260805")
