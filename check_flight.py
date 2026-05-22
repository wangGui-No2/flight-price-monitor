"""
机票价格变动监控 — 上海 ↔ 成都 往返
数据源：飞猪 flyai（含税总价）
价格有变动时推送飞书提醒（降价/涨价都通知）
"""

import os
import sys
import json
import traceback
import subprocess
import requests
from datetime import datetime

# ============ 配置 ============
ORIGIN = "上海"
DEST = "成都"
OUTBOUND_DATE = "2026-07-03"
RETURN_DATE = "2026-07-05"

OUTBOUND_BEFORE_HOUR = 10   # 去程不晚于 10:00
RETURN_AFTER_HOUR = 16      # 返程不早于 16:00

FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
HISTORY_FILE = "price_history.json"

SEARCH_URL = "https://www.google.com/travel/flights?q=Flights+from+Shanghai+to+Chengdu+on+2026-07-03+returning+2026-07-05+economy"


def search_flights():
    """通过飞猪 flyai CLI 搜索机票"""
    cmd = [
        "flyai", "search-flight",
        "--origin", ORIGIN,
        "--destination", DEST,
        "--dep-date", OUTBOUND_DATE,
        "--back-date", RETURN_DATE,
        "--sort-type", "3",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"flyai 无输出: {result.stderr}")

    data = json.loads(output)
    if data.get("status") != 0:
        raise RuntimeError(f"flyai 错误: {data.get('message')}")

    items = data.get("data", {}).get("itemList", [])

    results = []
    for idx, item in enumerate(items):
        try:
            journeys = item.get("journeys", [])
            out = journeys[0] if len(journeys) > 0 else {}
            ret = journeys[1] if len(journeys) > 1 else {}

            out_seg = out.get("segments", [{}])[0]
            ret_seg = ret.get("segments", [{}])[0]

            price = float(item.get("ticketPrice", 0))

            def safe_time(val):
                """安全提取时间字符串，兼容 str/dict 类型"""
                if isinstance(val, str) and val:
                    parts = val.split()
                    return parts[-1] if parts else val
                if isinstance(val, dict):
                    return str(val.get("time", "")) or ""
                return str(val) if val else ""

            out_dep_time = safe_time(out_seg.get("depDateTime", ""))
            out_hour = int(out_dep_time.split(":")[0]) if out_dep_time and ":" in out_dep_time else 99
            out_arr_time = safe_time(out_seg.get("arrDateTime", ""))

            out_airline = str(out_seg.get("marketingTransportName", "?"))
            out_no = str(out_seg.get("marketingTransportNo", "?"))

            ret_dep_time = safe_time(ret_seg.get("depDateTime", ""))
            ret_hour = int(ret_dep_time.split(":")[0]) if ret_dep_time and ":" in ret_dep_time else 99
            ret_arr_time = safe_time(ret_seg.get("arrDateTime", ""))

            ret_airline = str(ret_seg.get("marketingTransportName", "?"))
            ret_no = str(ret_seg.get("marketingTransportNo", "?"))

            dur_out = int(out.get("totalDuration", 0))
            dur_ret = int(ret.get("totalDuration", 0))
            jump_url = item.get("jumpUrl", "")

            results.append({
                "price": price,
                "out_airline": out_airline,
                "out_no": out_no,
                "out_dep_time": out_dep_time,
                "out_arr_time": out_arr_time,
                "out_hour": out_hour,
                "out_dur": dur_out,
                "ret_airline": ret_airline,
                "ret_no": ret_no,
                "ret_dep_time": ret_dep_time,
                "ret_arr_time": ret_arr_time,
                "ret_hour": ret_hour,
                "ret_dur": dur_ret,
                "outbound_ok": out_hour < OUTBOUND_BEFORE_HOUR,
                "return_ok": ret_hour >= RETURN_AFTER_HOUR,
                "jump_url": jump_url,
            })
        except Exception as e:
            print(f"  ⚠️ 解析第 {idx+1} 条结果失败: {e}")

    return results


def load_history():
    """加载历史价格记录"""
    try:
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"changes": []}


def save_history(history):
    """保存价格记录"""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def get_snapshot(results):
    """从当前结果提取关键价格快照"""
    perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]
    top5 = [r["price"] for r in results[:5]]
    return {
        "time": datetime.now().isoformat(),
        "cheapest": results[0]["price"] if results else None,
        "cheapest_airline": f"{results[0]['out_airline']} {results[0]['out_no']}" if results else None,
        "perfect_match": perfect[0]["price"] if perfect else None,
        "perfect_airline": f"{perfect[0]['out_airline']} {perfect[0]['out_no']}" if perfect else None,
        "top5": top5,
        "total_results": len(results),
        "perfect_count": len(perfect),
    }


def compare_and_alert(results):
    """对比历史价格，有变动则推送飞书"""
    if not results:
        return
    history = load_history()
    changes = history.get("changes", [])
    prev = changes[-1] if changes else None

    snapshot = get_snapshot(results)
    changes.append(snapshot)
    history["changes"] = changes
    save_history(history)

    # 首次运行，只记录不通知
    if prev is None:
        print("  首次记录，建立价格基线")
        send_baseline_card(snapshot, results)
        return

    # 检测变动
    alerts = []

    # 最低价变动
    prev_cheapest = prev.get("cheapest")
    curr_cheapest = snapshot["cheapest"]
    if prev_cheapest and curr_cheapest:
        diff = curr_cheapest - prev_cheapest
        if diff < 0:
            alerts.append({
                "type": "drop",
                "label": "💰 最低价下降",
                "detail": f"¥{prev_cheapest:,.0f} → ¥{curr_cheapest:,.0f}（↓¥{abs(diff):,.0f}）",
                "airline": snapshot["cheapest_airline"],
            })
        elif diff > 0:
            alerts.append({
                "type": "rise",
                "label": "📈 最低价回升",
                "detail": f"¥{prev_cheapest:,.0f} → ¥{curr_cheapest:,.0f}（↑¥{diff:,.0f}）",
                "airline": snapshot["cheapest_airline"],
            })

    # 匹配时间的最低航班变动
    prev_perfect = prev.get("perfect_match")
    curr_perfect = snapshot["perfect_match"]
    if prev_perfect and curr_perfect:
        diff = curr_perfect - prev_perfect
        if diff < 0:
            alerts.append({
                "type": "drop_match",
                "label": "✅ 匹配航班降价",
                "detail": f"¥{prev_perfect:,.0f} → ¥{curr_perfect:,.0f}（↓¥{abs(diff):,.0f}）",
                "airline": snapshot["perfect_airline"],
            })
        elif diff > 0:
            alerts.append({
                "type": "rise_match",
                "label": "⚠️ 匹配航班涨价",
                "detail": f"¥{prev_perfect:,.0f} → ¥{curr_perfect:,.0f}（↑¥{diff:,.0f}）",
                "airline": snapshot["perfect_airline"],
            })
    elif prev_perfect is None and curr_perfect is not None:
        alerts.append({
            "type": "new_match",
            "label": "🆕 出现匹配航班",
            "detail": f"最低 ¥{curr_perfect:,.0f}",
            "airline": snapshot["perfect_airline"],
        })
    elif prev_perfect is not None and curr_perfect is None:
        alerts.append({
            "type": "lost_match",
            "label": "❌ 匹配航班消失",
            "detail": "当前无符合时间要求的航班",
            "airline": None,
        })

    if alerts:
        print(f"  检测到 {len(alerts)} 项变动，推送通知")
        send_alert_card(snapshot, prev, alerts, results)
    else:
        print("  价格无变动，静默")


def send_baseline_card(snapshot, results):
    """首次运行：发送基线价格"""
    if not FEISHU_WEBHOOK:
        return

    now = datetime.now().strftime("%m-%d %H:%M")
    perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]

    match_line = ""
    if snapshot["perfect_match"]:
        match_line = f"\n✅ 匹配时间航班最低：**¥{snapshot['perfect_match']:,.0f}**（{snapshot['perfect_airline']}）"
    else:
        match_line = "\n⚠️ 当前无完全匹配时间的航班"

    top_lines = ""
    for i, r in enumerate(results[:8], 1):
        out_tag = "✓" if r["outbound_ok"] else "✗"
        ret_tag = "✓" if r["return_ok"] else "✗"
        top_lines += (
            f"\n{i}. ¥{r['price']:,.0f} {r['out_airline']} {r['out_no']} "
            f"去{r['out_dep_time']} {out_tag} 返{r['ret_dep_time']} {ret_tag}"
        )

    buy_url = perfect[0]["jump_url"] if perfect else results[0]["jump_url"]

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🔰 价格监控已启动"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**上海 ↔ 成都** | {OUTBOUND_DATE} → {RETURN_DATE}\n"
                            f"含税总价 | {now}\n"
                            f"最低总价：**¥{snapshot['cheapest']:,.0f}**（{snapshot['cheapest_airline']}）"
                            f"{match_line}\n\n"
                            f"**当前 TOP 8：**{top_lines}\n\n"
                            f"后续价格有任何变动都会自动通知"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "👉 飞猪购买"}, "type": "primary", "url": buy_url},
                    ],
                },
            ],
        },
    }

    try:
        requests.post(FEISHU_WEBHOOK, json=card, timeout=10)
        print("  ✅ 基线卡片发送成功")
    except Exception as e:
        print(f"  ❌ 发送失败: {e}")


def send_alert_card(snapshot, prev, alerts, results):
    """价格变动时推送"""
    if not FEISHU_WEBHOOK:
        return

    now = datetime.now().strftime("%m-%d %H:%M")
    perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]

    # 变动摘要
    alert_lines = "\n".join(f"**{a['label']}**：{a['detail']}" for a in alerts)

    # 当前价格概况
    summary = (
        f"当前最低：**¥{snapshot['cheapest']:,.0f}**（{snapshot['cheapest_airline']}）"
    )
    if snapshot["perfect_match"]:
        summary += f"\n匹配航班：**¥{snapshot['perfect_match']:,.0f}**（{snapshot['perfect_airline']}）"

    # TOP 5
    top_lines = ""
    for i, r in enumerate(results[:5], 1):
        out_tag = "✓" if r["outbound_ok"] else "✗"
        ret_tag = "✓" if r["return_ok"] else "✗"
        top_lines += f"\n{i}. ¥{r['price']:,.0f} {r['out_airline']} {r['out_no']} 去{r['out_dep_time']}{out_tag} 返{r['ret_dep_time']}{ret_tag}"

    buy_url = perfect[0]["jump_url"] if perfect else results[0]["jump_url"]

    # 根据是否有降价来选颜色
    has_drop = any(a["type"].startswith("drop") or a["type"] == "new_match" for a in alerts)
    template = "red" if has_drop else "orange"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "✈️ 机票价格变动"},
                "template": template,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**上海 ↔ 成都** | {OUTBOUND_DATE} → {RETURN_DATE} | {now}\n\n"
                            f"{alert_lines}\n\n"
                            f"{summary}\n\n"
                            f"**当前 TOP 5：**{top_lines}"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {"tag": "button", "text": {"tag": "plain_text", "content": "👉 飞猪购买"}, "type": "primary", "url": buy_url},
                        {"tag": "button", "text": {"tag": "plain_text", "content": "📊 查看历史"}, "type": "default", "url": "https://github.com/wangGui-No2/flight-price-monitor/blob/main/price_history.json"},
                    ],
                },
            ],
        },
    }

    try:
        requests.post(FEISHU_WEBHOOK, json=card, timeout=10)
        print("  ✅ 变动通知发送成功")
    except Exception as e:
        print(f"  ❌ 发送失败: {e}")


def main():
    print(f"[{datetime.now()}] 飞猪查价...")

    try:
        results = search_flights()
    except Exception as e:
        print(f"❌ 搜索失败: {e}")
        return

    print(f"  结果: {len(results)} 班")
    if results:
        perfect = [r for r in results if r["outbound_ok"] and r["return_ok"]]
        print(f"  最低: ¥{results[0]['price']:,.0f} ({results[0]['out_airline']} {results[0]['out_no']})")
        print(f"  匹配: {len(perfect)} 班" + (f" 最低 ¥{perfect[0]['price']:,.0f}" if perfect else ""))

    if results:
        compare_and_alert(results)
    else:
        print("  无结果，跳过")


if __name__ == "__main__":
    main()
