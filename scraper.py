"""
scraper-agent: Crawls UDN and ETtoday using Playwright (headless Chromium).
Outputs top 2 newest articles per source to data/news.json.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone, timedelta

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

TZ_TAIPEI = timezone(timedelta(hours=8))
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "data", "news.json")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
PAGE_TIMEOUT = 30_000  # ms


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)


def relative_to_iso(text: str) -> str:
    """Convert relative time strings like '3分鐘前' to ISO 8601."""
    now = now_taipei()
    text = text.strip()
    m = re.search(r"(\d+)\s*分鐘前", text)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*小時前", text)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat()
    # Try parsing absolute formats: "2026/04/17 10:30" or "04/17 10:30"
    for fmt in ("%Y/%m/%d %H:%M", "%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            return dt.replace(tzinfo=TZ_TAIPEI).isoformat()
        except ValueError:
            pass
    return now.isoformat()


def truncate_summary(text: str, max_len: int = 50) -> str:
    text = re.sub(r"\s+", "", text.strip())
    if len(text) > max_len:
        return text[:max_len] + "…"
    if len(text) < 30:
        return text
    return text


async def scrape_udn(page) -> list[dict]:
    """Crawl https://udn.com/news/breaknews/1 for top 2 articles."""
    results = []
    try:
        await page.goto("https://udn.com/news/breaknews/1", timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        # Wait for article list
        try:
            await page.wait_for_selector(".story-list__news, article, .story-list", timeout=PAGE_TIMEOUT)
        except PlaywrightTimeout:
            print("[udn] selector timeout, attempting parse anyway")

        articles = await page.query_selector_all(".story-list__news")
        if not articles:
            articles = await page.query_selector_all("article")

        for art in articles[:2]:
            try:
                # Title
                title_el = await art.query_selector("h2 a, .story-list__text h2")
                title = (await title_el.inner_text()).strip() if title_el else ""

                # Link
                link_el = await art.query_selector("a[href]")
                link = await link_el.get_attribute("href") if link_el else ""
                if link and link.startswith("/"):
                    link = "https://udn.com" + link

                # Time
                time_el = await art.query_selector(".story-list__time, time, .timestamp")
                raw_time = (await time_el.inner_text()).strip() if time_el else ""
                published_at = relative_to_iso(raw_time) if raw_time else now_taipei().isoformat()

                # Image
                img_el = await art.query_selector("img")
                image_url = await img_el.get_attribute("src") if img_el else ""
                if not image_url:
                    image_url = await page.evaluate(
                        "document.querySelector('meta[property=\"og:image\"]')?.content || ''"
                    )

                # Summary — try from paragraph in card, else use title
                summary_el = await art.query_selector("p, .story-list__text p")
                summary_raw = (await summary_el.inner_text()).strip() if summary_el else ""
                summary = truncate_summary(summary_raw or title)

                if title and link:
                    results.append({
                        "source": "聯合新聞網",
                        "source_id": "udn",
                        "title": title,
                        "image_url": image_url or "",
                        "published_at": published_at,
                        "summary": summary,
                        "link": link,
                    })
            except Exception as e:
                print(f"[udn] article parse error: {e}")

    except Exception as e:
        print(f"[udn] page error: {e}")

    print(f"[udn] scraped {len(results)} articles")
    return results


async def scrape_ettoday(page) -> list[dict]:
    """Crawl https://www.ettoday.net/news/news-list.htm for top 2 articles.
    ETtoday renders articles as <h3> blocks containing date+title+link.
    """
    results = []
    try:
        await page.goto("https://www.ettoday.net/news/news-list.htm", timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        try:
            await page.wait_for_selector("h3", timeout=10_000)
        except PlaywrightTimeout:
            print("[ettoday] h3 selector timeout, attempting parse anyway")

        h3_blocks = await page.query_selector_all("h3")

        for h3 in h3_blocks:
            if len(results) >= 2:
                break
            try:
                full_text = (await h3.inner_text()).strip()
                # Each news h3 has format: "YYYY/MM/DD HH:MM\n{category}\n{title}"
                link_el = await h3.query_selector("a[href]")
                if not link_el:
                    continue
                link = await link_el.get_attribute("href") or ""
                if not link or "/news/" not in link:
                    continue
                if link.startswith("/"):
                    link = "https://www.ettoday.net" + link

                title = (await link_el.inner_text()).strip()
                if not title or len(title) < 4:
                    continue

                # Parse time from text block (first line: YYYY/MM/DD HH:MM)
                lines = [l.strip() for l in full_text.splitlines() if l.strip()]
                raw_time = lines[0] if lines else ""
                published_at = relative_to_iso(raw_time)

                # Image and summary — fetch article page
                image_url = ""
                summary = ""
                try:
                    art_page = await page.context.new_page()
                    await art_page.goto(link, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
                    image_url = await art_page.evaluate(
                        "document.querySelector('meta[property=\"og:image\"]')?.content || ''"
                    )
                    summary = await art_page.evaluate(
                        "document.querySelector('meta[name=\"description\"]')?.content || ''"
                    )
                    await art_page.close()
                except Exception as e:
                    print(f"[ettoday] article fetch error: {e}")

                summary = truncate_summary(summary or title)

                results.append({
                    "source": "ETtoday 新聞雲",
                    "source_id": "ettoday",
                    "title": title,
                    "image_url": image_url,
                    "published_at": published_at,
                    "summary": summary,
                    "link": link,
                })
            except Exception as e:
                print(f"[ettoday] article parse error: {e}")

    except Exception as e:
        print(f"[ettoday] page error: {e}")

    print(f"[ettoday] scraped {len(results)} articles")
    return results


async def main():
    os.makedirs("data", exist_ok=True)
    all_items = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=UA)

        udn_page = await context.new_page()
        ettoday_page = await context.new_page()

        udn_items = await scrape_udn(udn_page)
        ettoday_items = await scrape_ettoday(ettoday_page)

        await browser.close()

    all_items = udn_items + ettoday_items
    all_items.sort(key=lambda x: x["published_at"], reverse=True)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Wrote {len(all_items)} items to {OUTPUT_PATH}")
    for item in all_items:
        print(f"  [{item['source_id']}] {item['title'][:40]}")


if __name__ == "__main__":
    asyncio.run(main())
