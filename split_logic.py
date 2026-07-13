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
    if station:
        # 지도 링크에는 추정 좌표 대신 실제 역 좌표를 사용 → 경로와 분기점이 일치
        fork["lat"], fork["lng"] = station["lat"], station["lng"]
    return fork


def compute_split(o: dict, a: dict, b: dict, hour: int | None = None) -> dict:
    """좌표 3개(출발 o, 목적지 a/b)로 합승 정산 계산.

    3가지 방식을 모두 비교해 가장 싼 방식을 추천:
    1. 분기점_환승: 분기점까지 같이 타고, 한 명이 내려서 갈아탐
    2. 경유_하차: 한 택시로 가까운 목적지 들러 내려주고, 그대로 먼 목적지까지 (환승 없음)
    3. 따로_타기: 위 방식들이 따로 타는 것보다 비싸면 따로 타라고 안내
    """
    dOA, dOB, dAB = road_km(o, a), road_km(o, b), road_km(a, b)
    s = max((dOA + dOB - dAB) / 2, 0.0)          # 겹치는 구간 거리
    a_alone, b_alone = estimate_fare(dOA, hour), estimate_fare(dOB, hour)
    separate_total = a_alone + b_alone

    PA, PB = max(dOA - s, 0.0), max(dOB - s, 0.0)  # 분기점 이후 각자 남은 거리
    cab_to_a, cab_to_b = estimate_fare(dOA, hour), estimate_fare(dOB, hour)

    # 시나리오 비교 — 환승형(분기점에서 한 명 하차 후 갈아탐) + 경유형(택시가 두 목적지 순서대로 방문)
    scenarios = []
    # 환승형 조건: 겹치는 구간이 절대적으로(0.4km↑) + 상대적으로(짧은 경로의 30%↑) 의미 있어야 함
    # → 방향이 반대인 두 목적지에 억지로 환승을 추천하는 것을 방지
    if s >= 0.4 and s >= 0.3 * min(dOA, dOB):
        scenarios += [
            {"type": "환승", "stay": "B", "off": "A", "mode": "택시",   "total": cab_to_b + estimate_fare(PA, hour)},
            {"type": "환승", "stay": "A", "off": "B", "mode": "택시",   "total": cab_to_a + estimate_fare(PB, hour)},
        ]
        # 대중교통 환승은 잔여 거리가 짧을 때만 현실적 (심야 막차·소요시간 고려)
        if PA <= 8.0:
            scenarios.append({"type": "환승", "stay": "B", "off": "A", "mode": "대중교통", "total": cab_to_b + TRANSIT_FARE})
        if PB <= 8.0:
            scenarios.append({"type": "환승", "stay": "A", "off": "B", "mode": "대중교통", "total": cab_to_a + TRANSIT_FARE})
    # 경유형: A 먼저 들르고 B까지 / B 먼저 들르고 A까지 (택시 1대, 환승 없음)
    scenarios += [
        {"type": "경유", "stay": "B", "off": "A", "mode": "없음(경유 하차)", "total": estimate_fare(dOA + dAB, hour)},
        {"type": "경유", "stay": "A", "off": "B", "mode": "없음(경유 하차)", "total": estimate_fare(dOB + dAB, hour)},
    ]
    best = min(scenarios, key=lambda x: x["total"])
    split = equal_savings_split(a_alone, b_alone, best["total"])

    # 차선책: 채택되지 않은 다른 유형 중 가장 싼 것 (예: 환승 대신 경유 하차하면 얼마인지)
    others = [x for x in scenarios if x["type"] != best["type"]]
    alt = min(others, key=lambda x: x["total"]) if others else None

    # off/stay 기호(A/B) → 실제 이름·지점 매핑
    dest_pt = {"A": a, "B": b}
    off_pt, stay_pt = dest_pt[best["off"]], dest_pt[best["stay"]]
    off_name, stay_name = off_pt["name"], stay_pt["name"]

    worth = split["총_절약액"] > 0  # 따로 타는 것보다 실제로 싸야만 합승 추천

    if not worth:
        # ⭐ 따로 타는 게 더 싸거나 같음 → 명확히 따로 타라고 안내
        방식 = "따로_타기"
        fork_label, fork = None, None
        안내 = (
            f"🚕 이 경우엔 합승보다 따로 타는 게 더 낫습니다. "
            f"합승 최저 비용 {best['total']:,}원 ≥ 따로 탈 때 {separate_total:,}원. "
            f"각자 택시를 타세요 — {a['name']} {a_alone:,}원, {b['name']} {b_alone:,}원."
        )
    elif best["type"] == "경유":
        # ⭐ 한 택시로 가까운 목적지 먼저 들르는 게 최선 (환승 없음)
        방식 = "경유_하차"
        fork = off_pt  # '갈라지는 지점' = 먼저 내리는 목적지 그 자체
        fork_label = off_name
        안내 = (
            f"👉 한 택시로 {o['name']}에서 출발해 {off_name}에 먼저 들러 한 명을 내려주고, "
            f"그대로 {stay_name}까지 갑니다. 갈아탈 필요가 없어 가장 편하고 저렴한 방식입니다."
        )
    else:
        방식 = "분기점_환승"
        fork = branch_point(o, a, b, s, dOA, dOB)
        fork_label = fork["역"] or "분기 지점(가까운 역 없음)"
        타는말 = "대중교통으로" if best["mode"] == "대중교통" else "택시로"
        안내 = (
            f"👉 {o['name']}에서 함께 택시를 타고 '{fork_label}' 부근까지 이동하세요. "
            f"거기서 {off_name} 가는 사람이 내려 {타는말} 갈아타고, "
            f"{stay_name} 가는 사람은 그대로 택시로 목적지까지 갑니다."
        )

    result = {
        "합승_추천": worth,
        "방식": 방식,                              # ⭐ 분기점_환승 | 경유_하차 | 따로_타기
        "분기점": fork_label,                     # 어디서 갈라지는지 (환승: 역 / 경유: 먼저 내리는 목적지)
        "분기점_안내": 안내,                        # ⭐ 누가 어디서 내려 어떻게 가는지
        "겹치는_구간_km": round(s, 2),
        "내리는_사람": off_name,
        "끝까지_타는_사람": stay_name,
        "갈아탈_교통수단": best["mode"] if worth else "해당없음(각자 이동)",
        "A_혼자": a_alone, "B_혼자": b_alone,
        **split,
        "출발": o["name"], "A_목적지": a["name"], "B_목적지": b["name"],
    }
    if worth:
        result["분기점_좌표"] = {"lat": round(fork["lat"], 6), "lng": round(fork["lng"], 6)}
        if alt is not None:
            방식명 = {"환승": "분기점 환승", "경유": "경유 하차"}
            result["대안"] = (
                f"{방식명[alt['type']]} 방식은 총 {alt['total']:,}원"
                f" ({'+' if alt['total'] >= best['total'] else '-'}{abs(alt['total'] - best['total']):,}원)"
            )
    else:
        # 따로 타기: 각자 그냥 내면 됨 (균등절약 정산 불필요)
        result.update({"A_지불": a_alone, "B_지불": b_alone, "A_절약": 0, "B_절약": 0})
    return result


def _pt(p: dict) -> str:
    return f"{quote(p['name'], safe='')},{p['lat']},{p['lng']}"


def kakao_map_route_url(points: list[dict], by: str = "car") -> str:
    """여러 지점을 순서대로 잇는 카카오맵 경로 링크.
    예: 출발지 → 분기점 → 목적지가 한 지도에 함께 보임.
    각 지점은 {"name", "lat", "lng"} dict.
    """
    return f"https://map.kakao.com/link/by/{by}/" + "/".join(_pt(p) for p in points)


def kakao_map_car_url(o: dict, dest: dict) -> str:
    return kakao_map_route_url([o, dest], by="car")


def kakao_map_point_url(name: str, lat: float, lng: float) -> str:
    """분기점 위치를 카카오맵에서 바로 보여주는 링크."""
    return f"https://map.kakao.com/link/map/{quote(name, safe='')},{lat},{lng}"
