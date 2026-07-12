"""
반반택시 — 분기점·정산 핵심 로직
- 카카오 Local API(키워드 검색)로 주소→좌표 (레퍼런스와 동일 패턴)
- 길찾기 API 없이 세 지점 거리만으로 '겹치는 구간(s)' 계산
    s = (d(O,A) + d(O,B) − d(A,B)) / 2   (Y자 공유 스템 근사)
- 균등 절약 정산 + 카카오맵/카카오T 링크
"""

from __future__ import annotations
import os
from urllib.parse import quote
from fare import estimate_fare, haversine_km, equal_savings_split, ROAD_FACTOR

KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
TRANSIT_FARE = 2500  # 심야버스/지하철 대략치 (확장에서 정교화)


def geocode(query: str) -> dict | None:
    """카카오 Local API로 장소명/주소 → 좌표. (레퍼런스와 동일 호출 패턴)"""
    import requests  # 지연 import
    api_key = os.getenv("KAKAO_REST_API_KEY")
    if not api_key:
        raise RuntimeError("KAKAO_REST_API_KEY is not set")
    headers = {"Authorization": f"KakaoAK {api_key}"}
    res = requests.get(KAKAO_KEYWORD_URL, headers=headers,
                       params={"query": query}, timeout=5)
    res.raise_for_status()
    docs = res.json().get("documents", [])
    if not docs:
        return None
    d = docs[0]
    return {"name": d["place_name"], "lat": float(d["y"]), "lng": float(d["x"])}


def road_km(p1: dict, p2: dict) -> float:
    return haversine_km(p1["lat"], p1["lng"], p2["lat"], p2["lng"]) * ROAD_FACTOR


def compute_split(o: dict, a: dict, b: dict, hour: int | None = None) -> dict:
    """좌표 3개(출발 o, 목적지 a/b)로 합승 정산 계산. (API 불필요, 순수 계산)"""
    dOA, dOB, dAB = road_km(o, a), road_km(o, b), road_km(a, b)
    s = max((dOA + dOB - dAB) / 2, 0.0)          # 겹치는 구간 거리
    a_alone, b_alone = estimate_fare(dOA, hour), estimate_fare(dOB, hour)

    PA, PB = max(dOA - s, 0.0), max(dOB - s, 0.0)  # 분기점 이후 각자 남은 거리
    cab_to_a, cab_to_b = estimate_fare(dOA, hour), estimate_fare(dOB, hour)

    # 4가지 시나리오: (누가 끝까지 타나, 내리는 사람 이동수단) → 합승 총비용
    scenarios = [
        {"stay": "B", "off": "A", "mode": "택시",   "total": cab_to_b + estimate_fare(PA, hour)},
        {"stay": "B", "off": "A", "mode": "대중교통", "total": cab_to_b + TRANSIT_FARE},
        {"stay": "A", "off": "B", "mode": "택시",   "total": cab_to_a + estimate_fare(PB, hour)},
        {"stay": "A", "off": "B", "mode": "대중교통", "total": cab_to_a + TRANSIT_FARE},
    ]
    best = min(scenarios, key=lambda x: x["total"])
    split = equal_savings_split(a_alone, b_alone, best["total"])

    worth = split["총_절약액"] > 0 and s >= 0.4  # 합승 이득 판단
    return {
        "합승_추천": worth,
        "겹치는_구간_km": round(s, 2),
        "최적_시나리오": f"{best['off']}가 분기점에서 {best['mode']}로 갈아탐, {best['stay']}는 끝까지",
        "A_혼자": a_alone, "B_혼자": b_alone,
        **split,
        "출발": o["name"], "A_목적지": a["name"], "B_목적지": b["name"],
    }


def kakao_map_car_url(o: dict, dest: dict) -> str:
    def pt(p):
        return f"{quote(p['name'], safe='')},{p['lat']},{p['lng']}"
    return f"https://map.kakao.com/link/by/car/{pt(o)}/{pt(dest)}"


def kakao_t_url(o: dict, dest: dict) -> str:
    # 카카오T 택시 호출용 딥링크 (출발/도착 좌표 전달)
    return (f"kakaotaxi://launch?sx={o['lng']}&sy={o['lat']}"
            f"&ex={dest['lng']}&ey={dest['lat']}")
