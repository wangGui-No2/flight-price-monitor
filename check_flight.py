"""
机票价格日报 — 上海 ↔ 成都 往返
数据源：飞猪 flyai（含税总价）
推送：飞书机器人日报
运行：GitHub Actions 定时触发
"""

import os
import json
import subprocess
import requests
from datetime import datetime

# ============ 配置 ============
ORIGIN = "上海"
DEST = "成都"
OUTBOUND_DATE = "2026-07-03"
RETURN_DATE = "2026-07-05"

# 时间要求
OUTBOUND_BEFORE_HOUR = 10    # 去程不晚于 10:00
RETURN_AFTER_HOUR = 16       # 返程不早于 16:00

# 飞书 Webhook
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# Google Flights 备用链接
SEARCH_URL = "https://www.google.com/travel/flights?q=Flights+from+Shanghai+to+Chengdu+on+2026-07-03+returning+2026-07-05+economy"


def search_flights():
    """通过飞猪 flyai CLI 搜索机票（含税总价）"""
    cmd = [
        "flyai", "search-flight",
        "--origin", ORIGIN,
        "--destination", DEST,
        "--dep-date", OUTBOUND_DATE,
        "--back-date", RETURN_DATE,
        "--sort-type", "3",       # 价格升序
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    # flyai 输出 JSON 到 stdout，提示信息到 stderr
    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"flyai 无输出，stderr: {result.stderr}")

    data = json.loads(output)
    if data.get("status") != 0:
        raise RuntimeError(f"flyai 错误: {data.get('message', 'unknown')}")

    system_msg = data.get("systemMessage", "")
    is_trial = "体验模式" in system_msg

    items = data.get("data", {}).get("itemList", [])

    results = []
    for item in items:
        journeys = item.get("journeys", [])
        out = journeys[0] if len(journeys) > 0 else {}
        ret = journeys[1] if len(journeys) > 1 else {}

        out_seg = out.get("segments", [{}])[0]
        ret_seg = ret.get("segments", [{}])[0]

        price = float(item.get("ticketPrice", 0))

        # 去程
        out_dep = out_seg.get("depDateTime", "")
        out_dep_time = out_dep.split()[-1] if out_dep else ""
        out_hour = int(out_dep_time.split(":")[0]) if out_dep_time else 99
        out_arr = (out_seg.get("arrDateTime", "").split()[-1]
                   if out_seg.get("arrDateTime") else "")
        out_airline = out_seg.get("marketingTransportName", "?")
        out_no = out_seg.get("marketingTransportNo", "?")
        out_dep_station = out_seg.get("depStationName", "?")
        out_arr_station = out_seg.get("arrStationName", "?")

        # 返程
        ret_dep = ret_seg.get("depDateTime", "")
        ret_dep_time = ret_dep.split()[-1] if ret_dep else ""
        ret_hour = int(ret_dep_time.split(":")[0]) if ret_dep_time else 99
        ret_arr = (ret_seg.get("arrDateTime", "").split()[-1]
                   if ret_seg.get("arrDateTime") else "")
        ret_airline = ret_seg.get("marketingTransportName", "?")
        ret_no = ret_seg.get("marketingTransportNo", "?")

        # 时长
        dur_out = int(out.get("totalDuration", 0))
        dur_ret = int(ret.get("totalDuration", 0))

        # 购买链接
        jump_url = item.get("jumpUrl", "")

        outbound_ok = out_hour < OUTBOUND_BEFORE_HOUR
        return_ok = ret_hour >= RETURN_AFTER_HOUR

        results.append({
            "price": price,
            "out_airline": out_airline,
            "out_no": out_no,
            "out_dep_time": out_dep_time,
            "out_arr_time": out_arr,
            "out_station": out_dep_station,
            "out_arr_station": out_arr_station,
            "out_hour": out_hour,
            "out_dur": dur_out,
            "ret_airline": ret_airline,
            "ret_no": ret_no,
            "ret_dep_time": ret_dep_time,
            "ret_arr_time": ret_arr,
            "ret_hour": ret_hour,
            "ret_dur": dur_ret,
            "outbound_ok": outbound_ok,
            "return_ok": return_ok,
            "jump_url": jump_url,
        })

    return results, is_trial


def send_feishu_card(results, is_trial):
    """发送飞书卡片日报"""
    if not FEISHU_WEBHOOK:
        print("⚠️ 未配置 FEISHU_WEBHOOK，跳过通知")
        return

    if not results:
        print("⚠️ 无搜索结果")
        return

    now = datetime.now().strftime("%m-%d %H:%M")
    cheapest = results[0]

    # 完全匹配时间的
    perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]

    # 标题
    if perfect:
        best = perfect[0]
        title = f"✈️ 机票日报 | 最低 ¥{cheapest['price']:,.0f} | 匹配 ¥{best['price']:,.0f}"
    else:
        title = f"✈️ 机票日报 | 最低 ¥{cheapest['price']:,.0f}"

    # 构建 TOP 8
    top_lines = ""
    for i, r in enumerate(results[:8], 1):
        out_tag = "✓" if r["outbound_ok"] else "✗"
        ret_tag = "✓" if r["return_ok"] else "✗"

        top_lines += (
            f"\n{i}. ¥**{r['price']:,.0f}** | {r['out_airline']} {r['out_no']} | "
            f"去{r['out_dep_time']} {out_tag} | 返{r['ret_dep_time']} {ret_tag} | "
            f"{r['out_dur']}m+{r['ret_dur']}m"
        )

    # 主文本
    perfect_info = ""
    if perfect:
        p_best = perfect[0]
        perfect_info = (
            f"\n✅ 完全匹配（去<{OUTBOUND_BEFORE_HOUR}:00 返≥{RETURN_AFTER_HOUR}:00）："
            f"**{len(perfect)} 班**，最低 **¥{p_best['price']:,.0f}**"
            f"（{p_best['out_airline']} {p_best['out_no']}）"
        )
    else:
        perfect_info = (
            f"\n⚠️ 已返回的航班中没有完全匹配时间要求的，建议放宽时间或查看完整结果"
        )

    trial_note = ""
    if is_trial:
        trial_note = (
            "\n\n📌 当前为体验模式，结果可能不全。"
            "注册 [飞猪AI开放平台](https://flyai.open.fliggy.com/) 获取正式API Key 解锁完整数据"
        )

    summary = (
        f"**上海 ↔ 成都** | {OUTBOUND_DATE} → {RETURN_DATE} | 经济舱（含税总价）\n"
        f"数据源：飞猪 | 查询时间：{now}\n"
        f"时间要求：去 < {OUTBOUND_BEFORE_HOUR}:00 | 返 ≥ {RETURN_AFTER_HOUR}:00"
        f"{perfect_info}\n\n"
        f"**TOP 8：**{top_lines}"
        f"{trial_note}"
    )

    # 首选购买链接：匹配时间的最低航班
    buy_url = perfect[0]["jump_url"] if perfect else results[0]["jump_url"]

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
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
                            "text": {"tag": "plain_text", "content": "👉 飞猪购买（含税价）"},
                            "type": "primary",
                            "url": buy_url,
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "🔗 Google Flights"},
                            "type": "default",
                            "url": SEARCH_URL,
                        },
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
    print(f"[{datetime.now()}] 通过飞猪 flyai 查询机票...")
    print(f"  航线: {ORIGIN} → {DEST} (往返)")
    print(f"  日期: {OUTBOUND_DATE} → {RETURN_DATE}")

    try:
        results, is_trial = search_flights()
    except Exception as e:
        print(f"❌ 搜索失败: {e}")
        return

    print(f"  结果: {len(results)} 班航班{' (体验模式)' if is_trial else ''}")
    if results:
        perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]
        print(f"  最低总价: ¥{results[0]['price']:,.0f} ({results[0]['out_airline']} {results[0]['out_no']})")
        print(f"  完全匹配时间: {len(perfect)} 班")
        if perfect:
            print(f"  匹配最低价: ¥{perfect[0]['price']:,.0f} ({perfect[0]['out_airline']} {perfect[0]['out_no']})")

    send_feishu_card(results, is_trial)


if __name__ == "__main__":
    main()
