import json
import urllib.request


def main():
    payload = {"message": "请用一句话说明该知识库最核心的主题。"}
    req = urllib.request.Request(
        "http://127.0.0.1:7860/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        text = resp.read().decode("utf-8")
        data = json.loads(text)
        print("used_notebooklm:", data.get("used_notebooklm"))
        print("has_answer:", bool(data.get("answer")))
        print("kb_summary:", (data.get("kb_summary") or "")[:160])
        print("kb_preview:", (data.get("kb_preview") or "")[:160])


if __name__ == "__main__":
    main()
