import os
import json
import base64
import logging
import traceback
from datetime import datetime

import boto3
from openai import OpenAI, OpenAIError
from linebot import LineBotApi, WebhookHandler
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from linebot.exceptions import LineBotApiError, InvalidSignatureError

# ---------- 環境変数 ----------
CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
TENANT_ID = 0

# ---------- 外部サービス ----------
# デフォルトで 15 秒タイムアウトを設定
client = OpenAI(api_key=OPENAI_API_KEY, timeout=15.0)
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)
dynamodb = boto3.resource('dynamodb')
chat_log_tb = dynamodb.Table('chat_log')
user_info_tb = dynamodb.Table('user_info')

# ---------- ロギング ----------
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------- キャラクター設定 ----------

def load_character_prompt():
    """character.py に system_prompt があれば取り込む"""
    try:
        from character import system_prompt
        return system_prompt
    except Exception:
        return "あなたは親切で賢いAIアシスタントです。"

# ---------- ユーティリティ ----------

def send_safe_reply(reply_token: str, text: str):
    """LINE の reply API を安全に呼び、例外を握りつぶさずログに残す"""
    try:
        # LINE の1メッセージ上限は5,000文字
        line_bot_api.reply_message(reply_token, TextSendMessage(text=text[:5000]))
    except LineBotApiError as e:
        logger.error(f"LINEBotApiError {e.status_code} {e.error.message}")
        logger.debug(traceback.format_exc())


def short_fallback(event):
    """致命的エラー時の簡易応答"""
    send_safe_reply(event.reply_token, "ごめんなさい、ただいま少しトラブルが発生しています。")

# ---------- Lambda ハンドラ ----------

def lambda_handler(event, context):
    logger.info({"action": "invoke", "aws_request_id": context.aws_request_id})

    # 署名取得（大小どちらでも）
    signature = event.get("headers", {}).get("x-line-signature") or \
                event.get("headers", {}).get("X-Line-Signature")

    # Body 取得（Base64 ⇔ プレーン）
    body = event.get("body", "")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")

    # 受信ペイロードを一部ログ
    logger.info({"action": "received_body", "body": body[:1000]})

    try:
        handler.handle(body, signature)
        return {"statusCode": 200, "body": "OK"}

    except InvalidSignatureError:
        logger.error("InvalidSignatureError: header mismatch")
        return {"statusCode": 400, "body": "Bad signature"}

    except Exception:
        logger.exception("Unhandled exception in lambda_handler")
        return {"statusCode": 500, "body": "Internal Error"}

# ---------- LINE Message ハンドラ ----------


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    start_ts = datetime.utcnow()
    
    # ソース情報を取得
    user_id = event.source.user_id
    source_type = event.source.type
    group_id = getattr(event.source, 'group_id', None) if source_type == 'group' else None
    room_id = getattr(event.source, 'room_id', None) if source_type == 'room' else None
    
    logger.info({
        "action": "handle_message", 
        "user_id": user_id,
        "source_type": source_type,
        "group_id": group_id,
        "room_id": room_id
    })

    try:
        # メッセージの前処理（グループの場合はメンションチェック含む）
        user_message, user_name = preprocess_message(event)
        
        # ユーザー情報を保存
        save_user_info(user_id, user_name)

        # 会話IDを決定（グループ/ルームの場合はそのID、個人の場合はユーザーID）
        conversation_id = group_id or room_id or user_id
        
        # 会話履歴を取得（会話IDごと）
        history = get_conversation_history(conversation_id)
        
        # OpenAI応答を生成
        ai_resp = get_openai_response(user_message, history)

        # 会話を保存（グループIDも含めて）
        save_conversation(
            conversation_id=conversation_id,
            actual_user_id=user_id,
            group_id=group_id,
            room_id=room_id,
            user_name=user_name,
            user_message=user_message,
            ai_response=ai_resp
        )
        
        # 返信
        send_safe_reply(event.reply_token, ai_resp)

    except ValueError as e:
        # メンションされていない場合など、意図的にスキップ
        if str(e) == "Bot not mentioned":
            logger.info("Bot not mentioned in group chat, skipping")
            return
        raise

    except OpenAIError as e:
        logger.error(f"OpenAIError: {e}")
        logger.debug(traceback.format_exc())
        short_fallback(event)

    except LineBotApiError as e:
        logger.error(f"LINEBotApiError: {e.status_code} {e.error.message}")
        logger.debug(traceback.format_exc())

    except Exception:
        logger.exception("Unhandled exception in handle_message")
        short_fallback(event)

    finally:
        elapsed = (datetime.utcnow() - start_ts).total_seconds()
        logger.info({"action": "finished", "elapsed_sec": elapsed})

# ---------- 前処理 ----------

def preprocess_message(event):
    msg_text = event.message.text
    user_id = event.source.user_id

    # グループチャットの場合はメンション必須
    if event.source.type == "group":
        bot_name = line_bot_api.get_bot_info().display_name
        if f"@{bot_name}" not in msg_text:
            raise ValueError("Bot not mentioned")
        msg_text = msg_text.replace(f"@{bot_name}", "").strip()

    # ユーザ名取得
    try:
        if event.source.type == "group":
            profile = line_bot_api.get_group_member_profile(event.source.group_id, user_id)
        elif event.source.type == "room":
            profile = line_bot_api.get_room_member_profile(event.source.room_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        user_name = profile.display_name
    except LineBotApiError:
        user_name = "Unknown"

    return msg_text, user_name

# ---------- OpenAI 呼び出し ----------

def get_openai_response(user_message, history):
    system_prompt = load_character_prompt()
    messages = [{"role": "system", "content": system_prompt}, *history,
                {"role": "user", "content": user_message}]

    # 呼び出し毎に個別タイムアウトを変えたい場合は timeout=XX も可
    response = client.chat.completions.create(
        model="gpt-4o-mini-2024-07-18",
        messages=messages,
        max_tokens=500,
        temperature=0.7,
        
    )
    return response.choices[0].message.content.strip()

# ---------- DynamoDB 操作 ----------

def save_user_info(user_id, user_name):
    now = datetime.utcnow().isoformat()
    try:
        user_info_tb.put_item(
            Item={
                "user_id": user_id,
                "user_name": user_name,
                "tenant_id": TENANT_ID,
                "updated_at": now,
                "created_at": now
            }
        )
    except Exception:
        logger.exception("Error saving user info")


def get_conversation_history(conversation_id):
    """会話履歴を取得（個人/グループ/ルームごとに分離）"""
    try:
        res = chat_log_tb.query(
            KeyConditionExpression="conversation_id = :cid",
            ExpressionAttributeValues={":cid": conversation_id},
            Limit=20,
            ScanIndexForward=False  # 新しい順
        )
        items = res.get("Items", [])
        items.reverse()

        history = []
        for item in items:
            # グループの場合は発言者名を含める
            user_content = item["user"]
            if item.get("user_name") and item.get("group_id"):
                user_content = f"{item['user_name']}: {user_content}"
            
            history.append({"role": "user", "content": user_content})
            history.append({"role": "assistant", "content": item["assistant"]})
        return history
    except Exception:
        logger.exception("Error getting conversation history")
        return []


def save_conversation(conversation_id, actual_user_id, group_id, room_id, 
                     user_name, user_message, ai_response):
    """会話を保存（グループIDを含む）"""
    try:
        date_ms = int(datetime.utcnow().timestamp() * 1000)
        
        item = {
            "conversation_id": conversation_id,  # PK（グループID/ルームID/ユーザーID）
            "date_ms": date_ms,
            "user": user_message,
            "assistant": ai_response,
            "user_name": user_name,
            "actual_user_id": actual_user_id,  # 実際の発言者
            "tenant_id": TENANT_ID,
            "created_at": datetime.utcnow().isoformat()
        }
        
        # グループIDがある場合は追加
        if group_id:
            item["group_id"] = group_id
        
        # ルームIDがある場合は追加
        if room_id:
            item["room_id"] = room_id
            
        chat_log_tb.put_item(Item=item)
        
    except Exception:
        logger.exception("Error saving conversation")