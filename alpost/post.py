import argparse
import datetime
import json
import sys
from pathlib import Path

from alpost.client import get_client

LOG_PATH = Path(__file__).resolve().parent.parent / "tweets_log.jsonl"


def log_post(text: str, tweet_id: str | None, error: str | None = None) -> None:
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "text": text,
        "tweet_id": tweet_id,
        "error": error,
    }
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def post(text: str) -> str:
    client = get_client()
    response = client.create_tweet(text=text)
    tweet_id = response.data["id"]
    log_post(text, tweet_id)
    return tweet_id


def main() -> None:
    parser = argparse.ArgumentParser(description="アルの自律ツイート投稿CLI")
    parser.add_argument("text", nargs="?", help="投稿する本文。省略時は標準入力から読む")
    args = parser.parse_args()

    text = args.text if args.text is not None else sys.stdin.read().strip()
    if not text:
        print("投稿する本文がありません", file=sys.stderr)
        sys.exit(1)

    try:
        tweet_id = post(text)
    except Exception as e:
        log_post(text, None, error=str(e))
        raise

    print(f"投稿完了 (id: {tweet_id}): {text[:30]}...")


if __name__ == "__main__":
    main()
