"""
机票价格日报 — 上海 ↔ 成都 往返
使用 SerpApi Google Flights API，每次运行推送飞书日报
运行环境：GitHub Actions 定时触发
"""

import os
import json
import requests
from datetime import datetime

# ============ 配置 ============
ORIGIN = "SHA,PVG"
DEST = "CTU,TFU"
OUTBOUND_DATE = "2026-07-03"
RETURN_DATE = "2026-07-05"

# 时间要求
OUTBOUND_BEFORE_HOUR = 10   # 去程不晚于10:00
RETURN_AFTER_HOUR = 16      # 返程不早于16:00

# API 配置
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# Google Flights 直达链接
SEARCH_URL = "https://www.google.com/travel/flights?q=Flights+from+Shanghai+to+Chengdu+on+2026-07-03+returning+2026-07-05+economy"


def search_flights():
    """通过 SerpApi 搜索 Google Flights"""
    params = {
        "engine": "google_flights",
        "departure_id": ORIGIN,
        "arrival_id": DEST,
        "outbound_date": OUTBOUND_DATE,
        "return_date": RETURN_DATE,
        "type": "1",              # 往返
        "travel_class": "1",      # 经济舱
        "currency": "CNY",
        "api_key": SERPAPI_KEY,
    }

    resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
    data = resp.json()

    if "error" in data:
        raise RuntimeError(f"SerpApi 错误: {data['error']}")

    all_groups = data.get("best_flights", []) + data.get("other_flights", [])

    results = []
    for g in all_groups:
        price = g.get("price")
        if not price or price <= 0:
            continue

        flights = g.get("flights", [])
        outbound = flights[0] if flights else {}
        ret = flights[1] if len(flights) > 1 else None

        # 去程信息
        dep_time = (outbound.get("departure_airport", {}).get("time", ""))
        dep_parts = dep_time.split()[-1] if dep_time else ""
        dep_hour = int(dep_parts.split(":")[0]) if dep_parts else 99

        dep_airport = outbound.get("departure_airport", {}).get("id", "?")
        arr_airport = outbound.get("arrival_airport", {}).get("id", "?")
        arr_time = (outbound.get("arrival_airport", {}).get("time", ""))
        arr_parts = arr_time.split()[-1] if arr_time else ""

        airline = outbound.get("airline", "?")
        flight_num = outbound.get("flight_number", "?")

        # 返程信息
        ret_parts = ""
        ret_hour = 99
        ret_arr_parts = ""
        ret_airline = ""
        ret_flight = ""
        if ret:
            ret_time = ret.get("departure_airport", {}).get("time", "")
            ret_parts = ret_time.split()[-1] if ret_time else ""
            ret_hour = int(ret_parts.split(":")[0]) if ret_parts else 99
            ret_arr_time = ret.get("arrival_airport", {}).get("time", "")
            ret_arr_parts = ret_arr_time.split()[-1] if ret_arr_time else ""
            ret_airline = ret.get("airline", "")
            ret_flight = ret.get("flight_number", "")

        total_duration = g.get("total_duration", 0)

        # 时间匹配判断
        outbound_ok = dep_hour < OUTBOUND_BEFORE_HOUR
        return_ok = ret_hour >= RETURN_AFTER_HOUR if ret else True

        results.append({
            "price": price,
            "airline": airline,
            "flight_num": flight_num,
            "dep_time": dep_parts,
            "dep_hour": dep_hour,
            "arr_time": arr_parts,
            "dep_airport": dep_airport,
            "arr_airport": arr_airport,
            "ret_airline": ret_airline,
            "ret_flight": ret_flight,
            "ret_time": ret_parts,
            "ret_hour": ret_hour,
            "ret_arr_time": ret_arr_parts,
            "duration": total_duration,
            "outbound_ok": outbound_ok,
            "return_ok": return_ok,
            "departure_token": g.get("departure_token", ""),
        })

    results.sort(key=lambda x: x["price"])
    return results


def send_feishu_card(results):
    """发送飞书卡片日报"""

    if not FEISHU_WEBHOOK:
        print("⚠️ 未配置 FEISHU_WEBHOOK，跳过通知")
        return

    if not results:
        print("⚠️ 无搜索结果，跳过通知")
        return

    now = datetime.now().strftime("%m-%d %H:%M")

    cheapest = results[0]

    # 时间条件匹配的航班
    perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]
    perfect_count = len(perfect)
    perfect_best = perfect[0] if perfect else None

    # 构建 TOP 8 列表
    top_lines = ""
    for i, r in enumerate(results[:8], 1):
        out_tag = "✓" if r["outbound_ok"] else "✗"
        ret_tag = "✓" if r["return_ok"] else "?"
        dur_h = r["duration"] // 60
        dur_m = r["duration"] % 60

        ret_info = f"返{r['ret_time']}" if r["ret_time"] else "返待定"
        top_lines += (
            f"\n{i}. ¥**{r['price']:,}** | {r['airline']} {r['flight_num']} | "
            f"去{r['dep_time']} {out_tag} | {ret_info} {ret_tag} | {dur_h}h{dur_m}"
        )

    # 主文本
    summary = (
        f"**上海 ↔ 成都** | {OUTBOUND_DATE} → {RETURN_DATE} | 经济舱\n"
        f"当前最低：**¥{cheapest['price']:,}**（{cheapest['airline']} {cheapest['flight_num']}）\n"
        f"时间要求：去 < {OUTBOUND_BEFORE_HOUR}:00 | 返 ≥ {RETURN_AFTER_HOUR}:00\n"
        f"完全匹配：**{perfect_count}** 班"
    )

    if perfect_best:
        summary += f"，最低 ¥{perfect_best['price']:,}（{perfect_best['airline']}）"

    summary += f"\n🕐 查询时间：{now}\n\n**TOP 8：**{top_lines}\n\n点击下方按钮查看详情和购票"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"✈️ 机票日报 | 最低 ¥{cheapest['price']:,}"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": summary},
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "👉 Google Flights 查看购买"},
                            "type": "primary",
                            "url": SEARCH_URL,
                        }
                    ],
                },
            ],
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=card, timeout=10)
        if resp.status_code == 200:
            print("✅ 飞书日报发送成功")
        else:
            print(f"❌ 飞书发送失败: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"❌ 飞书发送异常: {e}")


def main():
    print(f"[{datetime.now()}] 查询机票价格...")
    print(f"  航线: {ORIGIN} → {DEST} (往返)")
    print(f"  日期: {OUTBOUND_DATE} → {RETURN_DATE}")

    try:
        results = search_flights()
    except Exception as e:
        print(f"❌ 搜索失败: {e}")
        return

    print(f"  结果: {len(results)} 组航班")
    if results:
        perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]
        print(f"  最低: ¥{results[0]['price']:,} ({results[0]['airline']} {results[0]['flight_num']})")
        print(f"  完全匹配时间: {len(perfect)} 班")

    send_feishu_card(results)


if __name__ == "__main__":
    main()
