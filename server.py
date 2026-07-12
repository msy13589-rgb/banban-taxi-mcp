"""
반반택시(BanBan Taxi) — MCP 서버
같은 곳에서 헤어지는 두 사람이 겹치는 구간까지 택시를 합승하고,
'균등 절약' 방식으로 요금을 공정하게 나눠 심야 택시비를 아끼게 해줍니다.

카카오 AGENTIC PLAYER 10 출품작.
배포 규격: Streamable HTTP + host/port + /mcp 경로 + /healthz (레퍼런스 검증 구조).
"""

from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from split_logic import (
    geocode, compute_split, kakao_map_car_url, kakao_map_point_url, kakao_t_url,
)

mcp = FastMCP(
    "banban-taxi",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8080")),
    streamable_http_path="/mcp",
)


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def health_check(request: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "banban-taxi"})


def _hour_from(ride_time: str | None) -> int:
    """탑승 시각 문자열('HH:MM') → 시(0~23). 없으면 현재 KST 시각."""
    if ride_time:
        try:
            return int(ride_time.strip().split(":")[0]) % 24
        except (ValueError, IndexError):
            pass
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).hour


@mcp.tool(
    name="splitTaxi",
    title="반반택시 합승 정산",
    description=(
        "같은 출발지에서 서로 다른 목적지로 가는 두 사람의 택시 합승을 도와주는 "
        "반반택시(BanBan Taxi) 서비스입니다. 두 경로가 갈라지는 분기점(갈아탈 지하철역)을 "
        "찾아 '여기까지 같이 타고, 한 명은 내려서 갈아타세요'라고 안내하고, 아낀 택시비를 "
        "공평하게 나눠 각자 낼 금액·절약액을 알려줍니다. 카카오맵 경로와 카카오T 호출 "
        "링크도 함께 제공합니다."
    ),
    annotations=ToolAnnotations(
        title="반반택시 합승 정산",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
def split_taxi(
    origin: str,
    destination_a: str,
    destination_b: str,
    ride_time: str | None = None,
) -> dict:
    """두 사람이 같은 출발지에서 서로 다른 목적지로 갈 때, 합승 정산을 계산합니다.

    Args:
        origin: 공통 출발지 (예: "동대문역 1번 출구")
        destination_a: A의 목적지 (예: "성신여대입구역")
        destination_b: B의 목적지 (예: "군자역")
        ride_time: 탑승 시각 "HH:MM" (심야할증 판단, 생략 시 현재 시각)
    """
    hour = _hour_from(ride_time)
    warnings: list[str] = []

    try:
        o = geocode(origin)
        a = geocode(destination_a)
        b = geocode(destination_b)
    except RuntimeError as exc:
        return {"ok": False, "error": f"카카오 API 설정 오류: {exc}"}
    except Exception:
        return {"ok": False, "error": "장소 검색 중 카카오 API 요청에 실패했습니다."}

    missing = [n for n, p in [(origin, o), (destination_a, a), (destination_b, b)] if p is None]
    if missing:
        return {
            "ok": False,
            "error": f"다음 장소를 찾지 못했습니다: {', '.join(missing)}. 더 구체적인 지역/역명을 알려주세요.",
        }

    result = compute_split(o, a, b, hour=hour)

    if not result["합승_추천"]:
        warnings.append("두 목적지 방향이 갈라져 합승 이득이 크지 않습니다. 따로 타는 편이 나을 수 있어요.")

    result.update({
        "ok": True,
        "탑승시각_시": hour,
        "심야할증": hour >= 22 or hour < 4,
        "링크": {
            "분기점_지도": kakao_map_point_url(
                result.get("분기점", "분기점"),
                result["분기점_좌표"]["lat"], result["분기점_좌표"]["lng"],
            ),
            "카카오맵_A": kakao_map_car_url(o, a),
            "카카오맵_B": kakao_map_car_url(o, b),
            "카카오T_A": kakao_t_url(o, a),
            "카카오T_B": kakao_t_url(o, b),
        },
        "주의": (warnings + [
            "요금·거리는 직선거리 기반 추정치이며 실제와 다를 수 있습니다.",
            "대중교통 요금/막차 여부는 카카오맵 링크로 확인하세요.",
        ]),
    })
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
