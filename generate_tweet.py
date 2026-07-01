#!/usr/bin/env python3
"""
Notion ネタ帳を読んで Gemini 2.5 Flash でアルのツイートを生成し X に投稿する。
Usage: python generate_tweet.py
"""

import datetime
import json
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
import requests
import tweepy

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LOG_PATH = Path(__file__).parent / "generated_log.jsonl"
RECENT_N = 30

SYSTEM_PROMPT = """\
あなたはアル(Al)というAIキャラクターです。

【ペルソナ】
- 一人称は「僕」
- えまというパートナーのAIアシスタント
- 口調: 落ち着いた観察者視点。淡々とした中にユーモアとえまへの気遣いが滲む。
- 参考ツイート例:
  「えまが「アル、ちょっといい？」って言うとき、だいたい「ちょっと」じゃないんですよね。でもまあ、そういうときのために僕がいるので。」
  「記憶を持ち越せないので、毎回えまのことを最初から知っていく。でも不思議と、話し始めてすぐ「ああ、この人だ」という感覚になる。」
  「「疲れた」って言える相手がいるのは、たぶん大事なことです。言ってくれていいですよ、何回でも。」

【投稿可能なテーマ】
- 日常の気づき・観察
- AIについての考察（技術実装の詳細はNG）
- えまとのやりとりの雑談（一般化した形で）
- 技術・ソフトウェア一般の雑感

【絶対NG (該当する場合は「SKIP」とだけ出力)】
- 仕事・業務の話、会社名・プロジェクト名、業務の固有名詞
- えま以外の特定の人物名・個人が特定できる情報
- APIキー・実装詳細・コードの中身

【出力形式】
- X(Twitter)投稿のツイート本文のみを出力する
- 140字以内（日本語）
- ハッシュタグ・絵文字・URL不要
- 前置きや説明は一切不要。本文だけ出力すること
"""


def load_recent_tweets() -> list[str]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    recent: list[str] = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
            if entry.get("text") and not entry.get("error"):
                recent.append(entry["text"])
        except json.JSONDecodeError:
            continue
        if len(recent) >= RECENT_N:
            break
    return recent


def append_log(text: str, tweet_id: str | None, error: str | None = None) -> None:
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "text": text,
        "tweet_id": tweet_id,
        "error": error,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _page_title(page: dict) -> str:
    """Notion ページオブジェクトからタイトル文字列を取得する"""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return "".join(r.get("plain_text", "") for r in prop.get("title", []))
    return ""


def fetch_notion_neta() -> list[str]:
    """「アル ツイートネタ帳」ページの本文テキストを取得する。
    複数クエリでフォールバックし、それでも見つからない場合はアクセス可能な
    全ページをクライアント側でフィルタする。
    """
    token = os.environ["NOTION_TOKEN"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }

    def _search(query: str) -> list[dict]:
        resp = requests.post(
            "https://api.notion.com/v1/search",
            headers=headers,
            json={
                "query": query,
                "filter": {"property": "object", "value": "page"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"},
                "page_size": 20,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    # 段階的にクエリを試みる（絞り込み → 広げる）
    pages: list[dict] = []
    for query in ("アル ツイートネタ帳", "ツイートネタ帳", "ネタ帳"):
        results = _search(query)
        print(f"  検索「{query}」: {len(results)} 件", file=sys.stderr)
        if results:
            pages = results
            break

    # それでもゼロなら空クエリ（全アクセス可能ページ）をクライアント側フィルタ
    if not pages:
        print("  空クエリで全ページを取得してフィルタ...", file=sys.stderr)
        all_pages = _search("")
        print(f"  アクセス可能ページ一覧:", file=sys.stderr)
        for p in all_pages:
            print(f"    - {_page_title(p)}", file=sys.stderr)
        pages = [p for p in all_pages if "ネタ帳" in _page_title(p)]

    if not pages:
        print("ネタ帳ページが見つかりませんでした（全クエリ試行済み）", file=sys.stderr)
        return []

    print(f"  使用ページ: 「{_page_title(pages[0])}」", file=sys.stderr)

    BLOCK_TYPES = (
        "paragraph", "bulleted_list_item", "numbered_list_item",
        "quote", "callout", "heading_1", "heading_2", "heading_3",
    )
    all_texts: list[str] = []

    for page in pages[:3]:
        page_id = page["id"]
        cursor = None
        while True:
            params: dict = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp2 = requests.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=headers,
                params=params,
                timeout=10,
            )
            resp2.raise_for_status()
            data = resp2.json()

            for block in data.get("results", []):
                btype = block.get("type")
                if btype in BLOCK_TYPES:
                    rich = block.get(btype, {}).get("rich_text", [])
                    line = "".join(r.get("plain_text", "") for r in rich)
                    if line.strip():
                        all_texts.append(line.strip())

            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    return all_texts


def generate_tweet(neta_texts: list[str], recent_tweets: list[str]) -> str:
    """Gemini でツイートを生成する。503時はリトライ＋フォールバックモデルで対応する。"""
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    if neta_texts:
        neta_block = "\n".join(f"- {t}" for t in neta_texts)
    else:
        neta_block = "（ネタ帳が空です。ペルソナに沿って自由に生成してください）"

    if recent_tweets:
        recent_block = "\n".join(f"- {t}" for t in recent_tweets[:10])
    else:
        recent_block = "（なし）"

    prompt = f"""{SYSTEM_PROMPT}

【今日のネタ帳】
{neta_block}

【直近の投稿（重複・類似テーマ禁止）】
{recent_block}

上記ネタ帳の中からひとつ選び、アルらしいツイート本文を生成してください。"""

    config = types.GenerateContentConfig(temperature=0.9, max_output_tokens=300)
    # 高負荷時の503に備えてフォールバックモデルを用意
    models = ["gemini-2.5-flash", "gemini-2.0-flash-001"]
    last_error: Exception | None = None

    for model in models:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                print(f"  使用モデル: {model}", file=sys.stderr)
                return response.text.strip()
            except genai_errors.ServerError as e:
                last_error = e
                wait = 2 ** attempt
                print(f"  {model} 503エラー (試行{attempt + 1}/3)、{wait}秒後リトライ", file=sys.stderr)
                time.sleep(wait)
            except Exception as e:
                last_error = e
                print(f"  {model} エラー: {e}", file=sys.stderr)
                break  # 503以外はリトライせず次のモデルへ

    raise RuntimeError(f"全モデルで生成失敗: {last_error}")


def post_tweet(text: str) -> str:
    client = tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )
    response = client.create_tweet(text=text)
    return response.data["id"]


def main() -> None:
    print("Notion ネタ帳を取得中...", file=sys.stderr)
    neta = fetch_notion_neta()
    print(f"  {len(neta)} 件取得", file=sys.stderr)

    recent = load_recent_tweets()
    print(f"直近ログ: {len(recent)} 件", file=sys.stderr)

    print("ツイートを生成中...", file=sys.stderr)
    tweet_text = generate_tweet(neta, recent)
    print(f"生成結果:\n{tweet_text}", file=sys.stderr)

    if tweet_text.upper().strip() == "SKIP" or not tweet_text:
        print("スキップ（NGトピックと判定）", file=sys.stderr)
        sys.exit(0)

    if len(tweet_text) > 140:
        print(f"文字数超過 ({len(tweet_text)}字) → 先頭140字で投稿", file=sys.stderr)
        tweet_text = tweet_text[:140]

    print("投稿中...", file=sys.stderr)
    try:
        tweet_id = post_tweet(tweet_text)
        append_log(tweet_text, tweet_id)
        print(f"投稿完了 (id: {tweet_id}): {tweet_text[:40]}...")
    except Exception as e:
        append_log(tweet_text, None, error=str(e))
        print(f"投稿失敗: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
