import re
import requests
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from typing import Optional, Tuple, Any
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

app_token = os.environ["APP_TOKEN"]
bot_token=os.environ["BOT_TOKEN"]
dify_api_key = os.environ["DIFY_API_KEY"]
dify_base_url = os.environ["DIFY_BASE_URL"]

app = App(token=bot_token)

# thread -> dify conversation
thread_to_conversation: dict[str, str] = {}

def strip_mentions(text: str) -> str:
    return re.sub(r"<@[^>]+>", "", text).strip()

def call_dify_blocking(query: str, user_id: str, conversation_id: Optional[str]) -> Tuple[str, Optional[str]]:
    url = f"{dify_base_url}/v1/chat-messages"
    headers = {
        "Authorization": f"Bearer {dify_api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    payload: dict[str, Any] = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming",
        "user": user_id,
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id

    resp = requests.post(
        url,
        headers=headers,
        json=payload,
        stream=True,
        timeout=(10, 300),
    )
    resp.raise_for_status()

    events: list[dict] = []
    conv_id: Optional[str] = conversation_id

    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data:"):
            data_str = line[len("data:"):].strip()
            if data_str in ("[DONE]", "DONE"):
                break
            try:
                obj = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            events.append(obj)
            # keep updating conversation_id when it appears
            if isinstance(obj, dict) and obj.get("conversation_id"):
                conv_id = obj["conversation_id"]

    # 1) pick the LAST agent_thought (usually best)
    thought = ""
    for e in reversed(events):
        if e.get("event") == "agent_thought" and e.get("thought"):
            thought = e["thought"]
            break

    # 2) (optional fallback) reconstruct full answer from agent_message chunks
    answer_chunks = [
        e.get("answer", "")
        for e in events
        if e.get("event") == "agent_message" and isinstance(e.get("answer"), str)
    ]
    full_answer = "".join(answer_chunks).strip()

    # If you ONLY want thought, return thought (or empty string if none)
    # If you want something always, fallback to full_answer
    text_to_post = thought if thought else full_answer
    return text_to_post, conv_id

@app.event("app_mention")
def handle_mentions(event, say, logger):
    channel = event["channel"]
    ts = event["ts"]
    thread_ts = event.get("thread_ts") or ts
    slack_user = event.get("user", "unknown")
    query = strip_mentions(event.get("text", ""))

    if not query:
        say(text="Type your question after mentioning me", thread_ts=thread_ts)
        return

    thread_key = f"{channel}:{thread_ts}"
    conversation_id = thread_to_conversation.get(thread_key)

    try:
        answer, new_conversation_id = call_dify_blocking(query, slack_user, conversation_id)
        if new_conversation_id:
            thread_to_conversation[thread_key] = new_conversation_id
        if not answer.strip():
            answer = "(Dify returned an empty answer.)"

        # Reply in the same thread where the bot was mentioned
        say(text=answer, thread_ts=thread_ts)

    except requests.HTTPError as e:
        logger.exception(e)
        say(text=f"Dify HTTP error: {e}", thread_ts=thread_ts)
    except Exception as e:
        logger.exception(e)
        say(text=f"Unexpected error: {e}", thread_ts=thread_ts)

if __name__ == "__main__":
    SocketModeHandler(app, app_token).start()