"""
텔레그램 봇 chat_id 알아내기.

사용법:
    1. .env에 TELEGRAM_BOT_TOKEN 입력
    2. 텔레그램에서 BotFather가 알려준 본인 봇 username 검색
       (BotFather가 만들어준 봇 — BotFather 자체가 아님!)
    3. 그 봇과 채팅 시작 후 /start 또는 아무 메시지 전송
    4. python get_chat_id.py

출력된 chat_id를 .env의 TELEGRAM_CHAT_ID에 입력.
"""
from __future__ import annotations
import sys
import requests
import config


def main() -> int:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("[X] .env의 TELEGRAM_BOT_TOKEN이 비어있습니다.")
        print("    BotFather에게 받은 토큰 형태: 1234567890:ABCdef...")
        return 1

    print(f"[*] 토큰 확인: {token[:10]}...")
    print("[*] getUpdates 호출 중...")
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[X] 네트워크 오류: {e}")
        return 2

    if r.status_code != 200:
        print(f"[X] HTTP {r.status_code}: {r.text[:200]}")
        return 3

    data = r.json()
    if not data.get("ok"):
        print(f"[X] Telegram API 오류:")
        print(f"    {data}")
        if data.get("error_code") == 401:
            print("    → 토큰이 잘못됐습니다. BotFather에게 /mybots → 봇 선택 → API Token으로 재확인")
        return 4

    updates = data.get("result", [])
    if not updates:
        print()
        print("[!] 메시지가 없습니다. 다음을 확인하세요:")
        print()
        print("  1. 텔레그램에서 본인 봇 username으로 검색")
        print("     (BotFather가 'Done! Congratulations on your new bot. '")
        print("      문구와 함께 t.me/xxxxxBot 형태 링크를 줬을 것)")
        print()
        print("  2. 그 봇과 채팅 열고 /start 또는 '안녕' 같은 메시지 1건 전송")
        print()
        print("  3. 다시 이 스크립트 실행")
        print()
        print("  주의: BotFather에게 보낸 메시지는 여기 안 잡힘.")
        print("       반드시 BotFather가 만들어준 '새 봇'에게 보내야 함.")
        return 5

    chats: dict[int, dict] = {}
    for u in updates:
        msg = (u.get("message") or u.get("edited_message")
               or u.get("channel_post") or u.get("my_chat_member"))
        if not msg:
            continue
        c = msg.get("chat") or {}
        cid = c.get("id")
        if cid is None:
            continue
        chats[cid] = {
            "type": c.get("type"),
            "title": c.get("title") or "",
            "first_name": c.get("first_name") or "",
            "username": c.get("username") or "",
        }

    if not chats:
        print("[X] 메시지는 있지만 chat 정보를 추출하지 못함. 응답 원본:")
        print(data)
        return 6

    print()
    print("[OK] 발견된 채팅:")
    print()
    for cid, info in chats.items():
        label = info["title"] or info["first_name"] or info["username"] or "?"
        print(f"  chat_id = {cid}   ({info['type']}, {label})")
    print()
    if len(chats) == 1:
        only = next(iter(chats))
        print(f"→ .env에 다음을 입력하세요:")
        print(f"    TELEGRAM_CHAT_ID={only}")
    else:
        print("→ 여러 개라면 본인과의 1:1(type='private')을 .env에 입력하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
