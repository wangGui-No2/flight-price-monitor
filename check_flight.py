"""
机票价格自动监控 — 上海 ↔ 成都 往返
触发条件：总价低于 ¥2000 时，飞书机器人推送通知
运行环境：GitHub Actions 定时触发（无需本地开机）
"""

import os
import re
import sys
import json
import asyncio
import requests
from datetime import datetime
from playwright.async_api import async_playwright

# ============ 配置 ============
ORIGIN = "上海"
DEST = "成都"
OUTBOUND_DATE = "2026-07-03"
RETURN_DATE = "2026-07-05"
TARGET_PRICE = 2000  # 目标价（元）

# 时间要求
OUTBOUND_LATEST_HOUR = 10  # 去程不晚于上午10点出发
RETURN_EARLIEST_HOUR = 16  # 返程不早于下午4点出发

# 飞书 Webhook（从 GitHub Secrets 读取）
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")

# Google Flights 搜索 URL
SEARCH_URL = (
    "https://www.google.com/travel/flights?"
    "q=Flights+from+Shanghai+to+Chengdu+on+2026-07-03+returning+2026-07-05+economy"
)


async def main():
    print(f"[{datetime.now()}] 开始搜索...")
    print(f"  航线: {ORIGIN} → {DEST} (往返)")
    print(f"  日期: {OUTBOUND_DATE} → {RETURN_DATE}")
    print(f"  目标价: ≤ ¥{TARGET_PRICE}")

    results = []
    screenshot_path = None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = await context.new_page()

        # 打开 Google Flights
        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)  # 额外等待动态内容

        # 截图（方便调试）
        screenshot_path = "/tmp/flight_result.png"
        await page.screenshot(path=screenshot_path, full_page=False)

        # 尝试提取价格数据
        try:
            results = await extract_prices(page)
        except Exception as e:
            print(f"  提取价格失败: {e}")

        await browser.close()

    # 分析结果
    print(f"  提取到 {len(results)} 条结果")

    cheapest = None
    cheapest_price = float("inf")

    for r in results:
        if r["price"] < cheapest_price:
            cheapest_price = r["price"]
            cheapest = r

    if cheapest is None:
        print("  未找到符合条件的航班")
        return

    print(f"  最低价: ¥{cheapest['price']:.0f} ({cheapest['airline']})")

    # 判断是否触发通知
    if cheapest["price"] <= TARGET_PRICE:
        print(f"  ✅ 触发通知! 价格 ¥{cheapest['price']:.0f} ≤ 目标 ¥{TARGET_PRICE}")
        send_feishu_alert(cheapest, results)
    else:
        print(f"  ⏳ 未触发。最低价 ¥{cheapest['price']:.0f} > 目标 ¥{TARGET_PRICE}")
        # 可选：每天汇总一次（即使未触发）
        if os.environ.get("SEND_DAILY_SUMMARY", "false") == "true":
            send_daily_summary(cheapest, results)


async def extract_prices(page):
    """从 Google Flights 页面提取航班价格和基本信息"""

    # 等待价格元素出现
    try:
        await page.wait_for_selector(
            'div[role="list"] li[role="listitem"], '
            'li[class*="pIav2d"], '
            'div[class*="JMc5Hc"]',
            timeout=15000,
        )
    except Exception:
        print("  价格列表未在预期时间内加载，尝试备用选择器...")

    # 多种方式提取数据
    results = []

    # 方法1：通过页面 JS 提取结构化数据
    try:
        raw_data = await page.evaluate("""() => {
            const items = document.querySelectorAll('li[role="listitem"]');
            const flights = [];
            items.forEach(item => {
                const text = item.textContent || '';
                // 提取价格 — 匹配 ¥1,234 或 $123 格式
                const priceMatch = text.match(/[¥$]\\s*([0-9,]+)/g);
                if (priceMatch) {
                    const prices = priceMatch.map(p =>
                        parseInt(p.replace(/[¥$,]/g, '').replace(/\\s/g, ''))
                    );
                    flights.push({
                        text: text.slice(0, 200),
                        prices: prices,
                        fullText: text
                    });
                }
            });
            return flights;
        }""")

        for item in raw_data:
            if item["prices"]:
                # 取最小的有效价格（通常 > 50 元才是真实票价）
                valid_prices = [p for p in item["prices"] if p > 50]
                if valid_prices:
                    price = min(valid_prices)
                    # 尝试提取航司
                    airline = "未知航司"
                    for known in ["东方航空", "中国国航", "南方航空", "四川航空",
                                  "春秋航空", "吉祥航空", "海南航空", "成都航空",
                                  "厦门航空", "深圳航空", "中国联合航空"]:
                        if known in item["text"]:
                            airline = known
                            break
                    results.append({
                        "price": price,
                        "airline": airline,
                        "snippet": item["text"][:120],
                    })
    except Exception as e:
        print(f"  方法1提取失败: {e}")

    # 方法2：如果方法1失败，用正则从页面文本提取
    if not results:
        try:
            page_text = await page.evaluate("() => document.body.innerText")
            price_pattern = re.findall(r'[¥￥]\s*([\d,]+)', page_text)
            all_prices = [int(p.replace(",", "")) for p in price_pattern]
            # 过滤噪音 — 取 50~20000 之间的价格
            valid_prices = sorted(set(p for p in all_prices if 50 < p < 20000))
            if valid_prices:
                results.append({
                    "price": valid_prices[0],
                    "airline": "详见链接",
                    "snippet": "从页面提取的最低价格",
                })
                # 也加入前几个结果
                for p in valid_prices[1:5]:
                    results.append({
                        "price": p,
                        "airline": "详见链接",
                        "snippet": "",
                    })
        except Exception as e:
            print(f"  方法2提取失败: {e}")

    return results


def send_feishu_alert(cheapest, all_results):
    """发送飞书降价通知（卡片消息）"""

    if not FEISHU_WEBHOOK:
        print("  ⚠️ 未配置 FEISHU_WEBHOOK，跳过通知")
        return

    now = datetime.now().strftime("%m-%d %H:%M")

    # 构建其他结果信息
    other_lines = ""
    top5 = sorted(all_results, key=lambda x: x["price"])[:5]
    for i, r in enumerate(top5, 1):
        other_lines += f"\n{i}. {r['airline']} — ¥{r['price']:,}"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "✈️ 机票降价！快入手"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**上海 ↔ 成都 往返**\n"
                            f"📅 {OUTBOUND_DATE} → {RETURN_DATE}\n"
                            f"💰 当前最低：**¥{cheapest['price']:,}**（目标 ≤ ¥{TARGET_PRICE:,}）\n"
                            f"🕐 检查时间：{now}\n\n"
                            f"**TOP 5 价格：**{other_lines}\n\n"
                            f"⚠️ 去程需10:00前出发，返程需16:00后出发\n"
                            f"点击下方按钮查看详情和购买"
                        ),
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "👉 立即查看购买"},
                            "type": "primary",
                            "url": SEARCH_URL,
                            "value": {},
                        }
                    ],
                },
            ],
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=card, timeout=10)
        if resp.status_code == 200:
            print("  ✅ 飞书通知发送成功")
        else:
            print(f"  ❌ 飞书通知失败: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"  ❌ 飞书通知异常: {e}")


def send_daily_summary(cheapest, results):
    """发送每日汇总（即使价格未触发）"""
    if not FEISHU_WEBHOOK:
        return

    now = datetime.now().strftime("%m-%d %H:%M")
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🔍 机票价格日报"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**上海 ↔ 成都** | {OUTBOUND_DATE} → {RETURN_DATE}\n"
                            f"当前最低：**¥{cheapest['price']:,}** "
                            f"(距目标 ¥{TARGET_PRICE:,} 还差 ¥{cheapest['price'] - TARGET_PRICE:,})\n"
                            f"检查时间：{now}\n\n"
                            f"[查看详情]({SEARCH_URL})"
                        ),
                    },
                }
            ],
        },
    }

    try:
        requests.post(FEISHU_WEBHOOK, json=card, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
