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
    geocode, compute_split, kakao_map_route_url, kakao_map_point_url,
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
        "반반택시(BanBan Taxi) 서비스입니다. 3가지 방식을 비교해 가장 싼 방법을 추천합니다: "
        "① 분기점(갈아탈 역)까지 같이 타고 한 명이 갈아타는 '분기점 환승', "
        "② 한 택시로 가까운 목적지를 먼저 들러 내려주고 그대로 가는 '경유 하차', "
        "③ 합승이 오히려 손해면 '따로 타기'를 권합니다. 아낀 택시비는 공평하게 나눠 "
        "각자 낼 금액·절약액을 알려주고, 출발지·경유지·목적지가 한 지도에 함께 보이는 "
        "카카오맵 경로 링크도 제공합니다."
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

    # 내리는 사람 / 끝까지 타는 사람의 목적지 dict 매핑
    off_pt = a if result["내리는_사람"] == a["name"] else b
    stay_pt = b if off_pt is a else a

    방식 = result["방식"]
    if 방식 == "따로_타기":
        # ⭐ 따로 타는 게 더 쌈 → 각자 직행 경로만 제공
        links = {
            f"{a['name']}_경로": kakao_map_route_url([o, a]),
            f"{b['name']}_경로": kakao_map_route_url([o, b]),
        }
        warnings.append("합승보다 따로 타는 게 저렴합니다. 각자 택시를 타세요.")
    elif 방식 == "경유_하차":
        # ⭐ 택시 1대가 목적지 두 곳을 순서대로 방문 → 링크가 실제 이동 경로 그대로
        links = {
            "전체경로_지도": kakao_map_route_url([o, off_pt, stay_pt]),
            "먼저_내리는_곳": kakao_map_point_url(off_pt["name"], off_pt["lat"], off_pt["lng"]),
        }
    else:  # 분기점_환승
        fork_pt = {
            "name": result.get("분기점") or "분기점",
            "lat": result["분기점_좌표"]["lat"],
            "lng": result["분기점_좌표"]["lng"],
        }
        links = {
            # ⭐ 출발지→분기점→목적지 2곳이 모두 한 지도에 보이는 링크
            "전체경로_지도": kakao_map_route_url([o, fork_pt, off_pt, stay_pt]),
            # 실제 택시 이동 경로 (분기점 경유 → 끝까지 타는 사람 목적지)
            "합승택시_경로": kakao_map_route_url([o, fork_pt, stay_pt]),
            # 내리는 사람이 분기점에서 갈아탄 뒤 가는 경로
            "환승후_경로": kakao_map_route_url([fork_pt, off_pt]),
            "분기점_지도": kakao_map_point_url(fork_pt["name"], fork_pt["lat"], fork_pt["lng"]),
        }
        warnings.append("전체경로_지도의 마지막 구간(목적지1→목적지2)은 지점 표시용이며 실제 이동 경로가 아닙니다.")

    result.update({
        "ok": True,
        "탑승시각_시": hour,
        "심야할증": hour >= 22 or hour < 4,
        "링크": links,
        "주의": (warnings + [
            "요금·거리는 직선거리 기반 추정치이며 실제와 다를 수 있습니다.",
            "대중교통 요금/막차 여부는 카카오맵 링크로 확인하세요.",
            "택시 호출은 카카오맵 경로 화면의 '택시' 탭에서 카카오T로 바로 연결됩니다.",
        ]),
    })
    return result


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
