"""
반반택시 — 분기점·정산 핵심 로직
- 카카오 Local API(키워드 검색)로 주소→좌표 (레퍼런스와 동일 패턴)
- 길찾기 API 없이 세 지점 거리만으로 '겹치는 구간(s)' 계산
    s = (d(O,A) + d(O,B) − d(A,B)) / 2   (Y자 공유 스템 근사)
- 분기점(갈라지는 지점) 좌표 추정 + 가장 가까운 지하철역 이름 표기
- 균등 절약 정산 + 카카오맵/카카오T 링크
"""

from __future__ import annotations
import os
from urllib.parse import quote
from fare import estimate_fare, haversine_km, equal_savings_split, ROAD_FACTOR

KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
KAKAO_CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"
TRANSIT_FARE = 2500  # 심야버스/지하철 대략치 (확장에서 정교화)


def _headers() -> dict:
    api_key = os.getenv("KAKAO_REST_API_KEY")
    if not api_key:
        raise RuntimeError("KAKAO_REST_API_KEY is not set")
    return {"Authorization": f"KakaoAK {api_key}"}


def geocode(query: str) -> dict | None:
    """카카오 Local API로 장소명/주소 → 좌표. (레퍼런스와 동일 호출 패턴)"""
    import requests  # 지연 import
    res = requests.get(KAKAO_KEYWORD_URL, headers=_headers(),
                       params={"query": query}, timeout=5)
    res.raise_for_status()
    docs = res.json().get("documents", [])
    if not docs:
        return None
    d = docs[0]
    return {"name": d["place_name"], "lat": float(d["y"]), "lng": float(d["x"])}


def nearest_station(lat: float, lng: float) -> dict | None:
    """좌표 근처 가장 가까운 지하철역 (Kakao 카테고리 SW8). 분기점 이름 표기용."""
    import requests
    res = requests.get(KAKAO_CATEGORY_URL, headers=_headers(), params={
        "category_group_code": "SW8", "x": lng, "y": lat,
        "radius": 5000, "sort": "distance", "size": 1,
    }, timeout=5)
    res.raise_for_status()
    docs = res.json().get("documents", [])
    if not docs:
        return None
    d = docs[0]
    return {
        "name": d["place_name"],
        "lat": float(d["y"]), "lng": float(d["x"]),
        "dist_m": int(float(d.get("distance", 0) or 0)),
    }


def road_km(p1: dict, p2: dict) -> float:
    return haversine_km(p1["lat"], p1["lng"], p2["lat"], p2["lng"]) * ROAD_FACTOR


def _point_along(o: dict, dest: dict, dist_km: float, total_km: float) -> dict:
    """출발지 o에서 dest 방향으로 dist_km 지점의 좌표(직선 보간)."""
    t = 0.0 if total_km <= 0 else min(dist_km / total_km, 1.0)
    return {
        "lat": o["lat"] + (dest["lat"] - o["lat"]) * t,
        "lng": o["lng"] + (dest["lng"] - o["lng"]) * t,
    }


def branch_point(o: dict, a: dict, b: dict, s: float, dOA: float, dOB: float) -> dict:
    """분기점(두 경로가 갈라지는 지점) 좌표 추정 + 가장 가까운 역 이름."""
    # 공유 스템 길이 s 지점을, O→A / O→B 각각에서 잡아 평균 (Y자 갈래 근사)
    fa = _point_along(o, a, s, dOA)
    fb = _point_along(o, b, s, dOB)
    fork = {"lat": (fa["lat"] + fb["lat"]) / 2, "lng": (fa["lng"] + fb["lng"]) / 2}
    station = None
    try:
        station = nearest_station(fork["lat"], fork["lng"])
    except Exception:
        station = None
    fork["역"] = station["name"] if station else None
    fork["역까지_m"] = station["dist_m"] if station else None
    return fork


def compute_split(o: dict, a: dict, b: dict, hour: int | None = None) -> dict:
    """좌표 3개(출발 o, 목적지 a/b)로 합승 정산 계산. (순수 계산 + 분기점 역 조회)"""
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

    # 분기점: 두 목적지 이름을 실제로 매핑 (off/stay 는 A/B 기호)
    dest_name = {"A": a["name"], "B": b["name"]}
    off_name, stay_name = dest_name[best["off"]], dest_name[best["stay"]]

    fork = branch_point(o, a, b, s, dOA, dOB)
    fork_label = fork["역"] or "분기 지점(가까운 역 없음)"

    worth = split["총_절약액"] > 0 and s >= 0.4  # 합승 이득 판단
    타는말 = "대중교통으로" if best["mode"] == "대중교통" else "택시로"
    if worth:
        안내 = (
            f"👉 {o['name']}에서 함께 택시를 타고 '{fork_label}' 부근까지 이동하세요. "
            f"거기서 {off_name} 가는 사람이 내려 {타는말} 갈아타고, "
            f"{stay_name} 가는 사람은 그대로 택시로 목적지까지 갑니다."
        )
    else:
        안내 = "두 목적지 방향이 거의 바로 갈라져 합승 이득이 크지 않아요. 따로 타는 편이 나을 수 있어요."

    return {
        "합승_추천": worth,
        "분기점": fork_label,                     # ⭐ 어디서 갈라지는지 (역 이름)
        "분기점_좌표": {"lat": round(fork["lat"], 6), "lng": round(fork["lng"], 6)},
        "분기점_안내": 안내,                        # ⭐ 누가 어디서 내려 뭘로 갈아타는지
        "겹치는_구간_km": round(s, 2),
        "내리는_사람": off_name,
        "끝까지_타는_사람": stay_name,
        "갈아탈_교통수단": best["mode"],
        "A_혼자": a_alone, "B_혼자": b_alone,
        **split,
        "출발": o["name"], "A_목적지": a["name"], "B_목적지": b["name"],
    }


def kakao_map_car_url(o: dict, dest: dict) -> str:
    def pt(p):
        return f"{quote(p['name'], safe='')},{p['lat']},{p['lng']}"
    return f"https://map.kakao.com/link/by/car/{pt(o)}/{pt(dest)}"


def kakao_map_point_url(name: str, lat: float, lng: float) -> str:
    """분기점 위치를 카카오맵에서 바로 보여주는 링크."""
    return f"https://map.kakao.com/link/map/{quote(name, safe='')},{lat},{lng}"


def kakao_t_url(o: dict, dest: dict) -> str:
    # 카카오T 택시 호출용 딥링크 (출발/도착 좌표 전달)
    return (f"kakaotaxi://launch?sx={o['lng']}&sy={o['lat']}"
            f"&ex={dest['lng']}&ey={dest['lat']}")
