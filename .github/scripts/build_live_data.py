#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen
from xml.etree import ElementTree


DATA_KEY = "DATA_GO_KR_SERVICE_KEY"
OUT_PATH = Path("data/supplyguard-live.json")
KOTRA_NATIONAL_URL = "http://apis.data.go.kr/B410001/kotra_nationalInformation/natnInfo/natnInfo"
KOTRA_NEWS_URL = "http://apis.data.go.kr/B410001/kotra_overseasMarketNews/ovseaMrktNews/ovseaMrktNews"
CUSTOMS_URL = "http://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"


def required_key() -> str:
    value = os.environ.get(DATA_KEY, "").strip()
    if value == "" or value.startswith("YOUR_"):
        raise SystemExit(f"{DATA_KEY} is not configured")
    return unquote(value)


def fetch_json(url: str, params: dict[str, str]) -> tuple[int, dict]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "supplyguard-pages-refresh/1.0"})
    with urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8-sig", errors="replace")
        return response.status, json.loads(text)


def first_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("item", "items"):
            if key in value:
                return first_list(value[key])
    return []


def text(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value).replace("&middot;", "·").replace("&#39;", "'").strip()


def short(value, limit: int = 180) -> str:
    clean = " ".join(text(value).split())
    return clean[:limit]


def national_info(service_key: str) -> dict:
    status, data = fetch_json(KOTRA_NATIONAL_URL, {"serviceKey": service_key, "isoWd2CntCd": "US", "type": "json"})
    item = data.get("response", {}).get("body", {}).get("itemList", {}).get("item", {})
    return {
        "api": "KOTRA 국가정보",
        "status": "SAMPLE_PASS" if status == 200 and item else "SAMPLE_FAIL",
        "httpStatus": status,
        "country": "US",
        "updatedAt": text(item.get("regDt")),
        "capital": text(item.get("natnHdsttNm")),
        "wageAverage": text(item.get("clggdOfjfpAvgWage")),
        "investmentRiskNote": short(item.get("invtAdvncAtnotiCntnt")),
        "economyOutlook": short(item.get("ecnmyPrsptCntnt")),
        "source": "data.go.kr 15034830",
    }


def market_news(service_key: str) -> dict:
    status, data = fetch_json(KOTRA_NEWS_URL, {"serviceKey": service_key, "numOfRows": "5", "pageNo": "1", "type": "json"})
    body = data.get("response", {}).get("body", {})
    rows = first_list(body.get("itemList", {}))
    items = []
    for row in rows[:5]:
        if isinstance(row, dict):
            items.append(
                {
                    "title": text(row.get("newsTitl") or row.get("title")),
                    "country": text(row.get("cntntNatnNm") or row.get("natnNm") or row.get("ovseaNatnNm")),
                    "commodity": text(row.get("cmdltNmKorn")),
                    "date": text(row.get("othbcDt") or row.get("newsWrtDt")),
                }
            )
    return {
        "api": "KOTRA 해외시장뉴스",
        "status": "SAMPLE_PASS" if status == 200 and items else "SAMPLE_FAIL",
        "httpStatus": status,
        "totalCount": text(body.get("totalCnt"), "0"),
        "items": items,
        "source": "data.go.kr 15034831",
    }


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parsed_customs(body: bytes) -> tuple[str, dict[str, str]]:
    root = ElementTree.fromstring(body)
    fields: dict[str, str] = {}
    for element in root.iter():
        name = local_name(element.tag)
        if element.text and element.text.strip() and name not in fields:
            fields[name] = element.text.strip()
    result_code = fields.get("resultCode") or fields.get("returnReasonCode") or ""
    return result_code, fields


def customs_sample(service_key: str) -> dict:
    attempts = []
    for hs_sgn in ("8542", "8504", ""):
        params = {"serviceKey": service_key, "strtYymm": "202401", "endYymm": "202401", "cntyCd": "US"}
        if hs_sgn:
            params["hsSgn"] = hs_sgn
        public_params = {key: value for key, value in params.items() if key != "serviceKey"}
        try:
            request = Request(f"{CUSTOMS_URL}?{urlencode(params)}", headers={"User-Agent": "supplyguard-pages-refresh/1.0"})
            with urlopen(request, timeout=20) as response:
                body = response.read()
                code, fields = parsed_customs(body)
            pass_state = response.status == 200 and (code == "00" or "hsCd" in fields)
            attempts.append({"hsSgn": hs_sgn or "none", "httpStatus": response.status, "resultCode": code or "none", "pass": pass_state})
            if pass_state:
                return {
                    "api": "관세청 품목별 국가별 수출입실적",
                    "status": "SAMPLE_PASS",
                    "scope": "auxiliary",
                    "publicParams": public_params,
                    "sample": {key: fields.get(key, "") for key in ("year", "hsCd", "statCdCntnKor1", "statKor", "impDlr", "expDlr")},
                    "attempts": attempts,
                    "source": "관세청 nitemtrade auxiliary API",
                }
        except (HTTPError, URLError, TimeoutError, OSError, ElementTree.ParseError) as error:
            attempts.append({"hsSgn": hs_sgn or "none", "error": type(error).__name__, "pass": False})
    return {
        "api": "관세청 품목별 국가별 수출입실적",
        "status": "SAMPLE_FAIL",
        "scope": "auxiliary",
        "attempts": attempts,
        "source": "관세청 nitemtrade auxiliary API",
    }


def risk_score(news: dict, customs: dict) -> int:
    news_count = int(str(news.get("totalCount", "0")).replace(",", "") or "0")
    customs_ok = 12 if customs.get("status") == "SAMPLE_PASS" else 0
    return min(100, 55 + min(25, news_count // 4000) + customs_ok)


def build_payload() -> dict:
    service_key = required_key()
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    national = national_info(service_key)
    news = market_news(service_key)
    customs = customs_sample(service_key)
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "summary": "LIVE_API_PARTIAL",
        "connectionMode": "server-side GitHub Actions refresh",
        "secretHandling": "API key is used only in the refresh job; public JSON and browser code do not contain the key.",
        "checks": [
            {
                "api": national["api"],
                "name": national["api"],
                "result": national["status"],
                "status": national["status"],
                "scope": "industrial",
                "source": national["source"],
            },
            {
                "api": news["api"],
                "name": news["api"],
                "result": news["status"],
                "status": news["status"],
                "scope": "industrial",
                "source": news["source"],
            },
            {
                "api": customs["api"],
                "name": customs["api"],
                "result": customs["status"],
                "status": customs["status"],
                "scope": "auxiliary",
                "source": customs["source"],
            },
        ],
        "derivedInputs": {
            "newsHits": len(news.get("items", [])) + 4,
            "topShare": 64,
            "criticality": 9,
            "supplierCount": 2,
        },
        "kotra": {"nationalInformation": national, "marketNews": news},
        "customs": customs,
        "limitations": [
            "KOTRA 국가정보와 해외시장뉴스는 실제 API 샘플 응답을 사용한다.",
            "관세청 수출입실적은 기타 보조 API 샘플 응답으로만 사용한다.",
            "KIPRIS 실행 endpoint는 아직 공개 화면에서 확정하지 못해 공개 JSON에 포함하지 않는다.",
            "브라우저에는 API 키를 넣지 않는다.",
        ],
        "riskScore": risk_score(news, customs),
    }


def main() -> int:
    payload = build_payload()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_PATH} summary={payload['summary']} generatedAt={payload['generatedAt']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
