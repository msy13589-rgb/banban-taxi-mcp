"""
반반택시 — 요금·정산 계산 엔진 (외부 API 불필요, 순수 규칙 기반)

서울 중형택시 기준 (2023.2 개정, 2026 현재 유지):
- 기본요금 4,800원 / 1.6km
- 이후 거리요금 131m당 100원
- 심야할증: 22~04시. 23~02시 40%, 그 외(22~23, 02~04) 20%
"""

from __future__ import annotations
import math

BASE_FARE = 4800          # 기본요금
BASE_DISTANCE_KM = 1.6    # 기본거리
UNIT_DISTANCE_M = 131     # 거리요금 단위(m)
UNIT_FARE = 100           # 단위당 요금(원)
ROAD_FACTOR = 1.3         # 직선거리 → 실제 도로거리 보정


def night_surcharge_rate(hour: int) -> float:
    """탑승 시각(0~23시)의 심야할증률."""
    if 23 <= hour or hour < 2:
        return 0.4
    if 22 <= hour < 23 or 2 <= hour < 4:
        return 0.2
    return 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 좌표 사이 직선거리(km)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def estimate_fare(distance_km: float, hour: int | None = None) -> int:
    """거리(km) → 예상 택시요금(원). hour 주면 심야할증 반영."""
    d = max(distance_km, 0.0)
    if d <= BASE_DISTANCE_KM:
        day = BASE_FARE
    else:
        extra_m = (d - BASE_DISTANCE_KM) * 1000
        units = math.ceil(extra_m / UNIT_DISTANCE_M)
        day = BASE_FARE + units * UNIT_FARE
    rate = night_surcharge_rate(hour) if hour is not None else 0.0
    fare = day * (1 + rate)
    return int(round(fare / 100.0) * 100)  # 100원 단위 반올림


def equal_savings_split(a_alone: int, b_alone: int, join_total: int) -> dict:
    """
    '균등 절약' 정산.
    - a_alone, b_alone: 각자 혼자 탈 때 요금
    - join_total: 합승 시나리오의 총 비용(공유구간 + 각자 이후구간; 중간하차자 대중교통/새택시 포함)
    반환: 각자 낼 금액, 절약액 등
    """
    separate_total = a_alone + b_alone
    total_savings = separate_total - join_total
    per_savings = total_savings // 2  # 절약을 반씩 (원 단위 내림)
    a_pay = max(a_alone - per_savings, 0)
    b_pay = max(b_alone - per_savings, 0)
    # 반올림/하한 처리로 합이 어긋나면 끝까지 가는(더 내는) 쪽에서 보정
    diff = join_total - (a_pay + b_pay)
    if a_pay >= b_pay:
        a_pay += diff
    else:
        b_pay += diff
    return {
        "따로_탈때_합계": separate_total,
        "합승_총비용": join_total,
        "총_절약액": total_savings,
        "A_지불": a_pay,
        "B_지불": b_pay,
        "A_절약": a_alone - a_pay,
        "B_절약": b_alone - b_pay,
        "합승_이득": total_savings > 0,
    }
