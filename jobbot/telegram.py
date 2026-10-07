"""Bot transport; errors omit remote descriptions and token-bearing URLs."""
import json
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
from .websource import NoRedirect


class TelegramError(Exception):
    def __init__(self, kind, retry_after=0):
        super().__init__(kind)  # Never include exception URL, token or remote description.
        self.kind = kind
        self.retry_after = retry_after


class BotAPI:
    def __init__(self, token):
        if not token or any(c.isspace() for c in token):
            raise ValueError("Invalid token")
        self._token = token

    def call(self, method, payload=None, files=None):
        if method not in {"getMe", "getWebhookInfo", "sendMessage", "getUpdates", "getChat", "sendDocument"}:
            raise ValueError("Unsupported method")
        content_type='application/json'
        data=json.dumps(payload or {}).encode()
        if files:
            if method!='sendDocument': raise ValueError('Unexpected upload')
            boundary='jobbot_'+secrets.token_hex(24)
            chunks=[]
            for key,value in (payload or {}).items():
                if not re.fullmatch(r'[a-z_]+',key): raise ValueError('Invalid field')
                encoded=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{encoded}\r\n'.encode())
            for key,filename,mime,body in files:
                if not re.fullmatch(r'[a-z_]+',key) or not re.fullmatch(r'[A-Za-z0-9_.-]+',filename):
                    raise ValueError('Invalid attachment name')
                if mime != 'text/plain; charset=utf-8' or len(body)>20_000:
                    raise ValueError('Invalid attachment')
                chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),body,b'\r\n']
            chunks.append(f'--{boundary}--\r\n'.encode())
            data=b''.join(chunks)
            content_type=f'multipart/form-data; boundary={boundary}'
        req = Request(f"https://api.telegram.org/bot{self._token}/{method}",
                      data=data, headers={"Content-Type":content_type})
        try:
            try:
                response = build_opener(NoRedirect).open(req, timeout=60 if files else 20)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(1_000_001)
            if status >= 500:
                raise TelegramError("uncertain")
            data = json.loads(raw)
            if data.get("ok") is not True:
                code = data.get("error_code", status)
                if code == 429:
                    retry = data.get("parameters", {}).get("retry_after", 60)
                    raise TelegramError("rate_limit", max(1, int(retry)))
                raise TelegramError("rejected")
            return data["result"]
        except (URLError, TimeoutError, OSError, ValueError, KeyError):
            raise TelegramError("uncertain") from None

    def preflight(self, expected_username=None):
        me = self.call("getMe")
        if expected_username and me.get("username", "").lower() != expected_username.lstrip("@").lower():
            raise TelegramError("wrong_bot")
        if me.get("is_bot") is not True or not me.get("username"):
            raise TelegramError("invalid_bot_identity")
        info = self.call("getWebhookInfo")
        if info.get("url"):
            raise TelegramError("webhook_exists")
        # An empty webhook does not prove absence of another getUpdates consumer.
        return me

    def send(self, chat_id, body):
        if type(chat_id) is not int or chat_id <= 0:
            raise ValueError("Expected a confirmed personal chat ID")
        if not body or len(body.encode("utf-16-le")) // 2 > 4096:
            raise ValueError("Invalid message length")
        result = self.call("sendMessage", {
            "chat_id": chat_id, "text": body, "link_preview_options": {"is_disabled": True},
            "allow_paid_broadcast": False,
        })
        if type(result.get("message_id")) is not int:
            raise TelegramError("uncertain")
        return result["message_id"]

    def send_letter(self, chat_id, packet, reply_to):
        if type(chat_id) is not int or chat_id<=0 or type(reply_to) is not int or reply_to<=0:
            raise ValueError('Expected confirmed chat and card')
        letter=packet['letter'].encode('utf-8-sig')  # Readable in Windows Notepad as well.
        if not 100<=len(letter)<=20_000: raise ValueError('Invalid letter')
        result=self.call('sendDocument',{
            'chat_id':chat_id,'document':'attach://letter','disable_content_type_detection':True,
            'reply_parameters':{'message_id':reply_to},'disable_notification':True,'allow_paid_broadcast':False,
        },files=[('letter',packet['letter_filename'],'text/plain; charset=utf-8',letter)])
        if (not isinstance(result,dict) or type(result.get('message_id')) is not int
                or result.get('chat',{}).get('id')!=chat_id):
            raise TelegramError('uncertain')
        return [result['message_id']]


def deliver_one(store, api, chat_id, now, asset_dir=None):
    """Deliver at most one item, with persistent global pacing for the personal chat."""
    if store.get_setting("delivery_halted"):
        return "halted"
    if float(store.get_setting("send_after", "0")) > now:
        return "waiting"
    entry = store.claim(now)
    if not entry:
        return "empty"
    packet=json.loads(entry['packet']) if entry.get('packet') and (store.packets_enabled or entry['message_id'] is not None) else None
    try:
        if packet and entry['message_id'] is not None:
            attachment_ids=api.send_letter(chat_id,packet,entry['message_id'])
            store.resolve(entry['key'],'sent',pause_until=now+2,attachment_ids=attachment_ids)
            return 'sent'
        message_id = api.send(chat_id, entry["body"])
    except TelegramError as error:
        if error.kind == "rate_limit":
            store.resolve(entry["key"], "pending", now + error.retry_after, pause_until=now + error.retry_after)
            return "rate_limit"
        outcome = "uncertain" if error.kind == "uncertain" else "failed"
        store.resolve(entry["key"], outcome, pause_until=now + 60, halt=outcome)
        return outcome
    except Exception:
        store.resolve(entry["key"], "uncertain", pause_until=now + 60, halt="uncertain")
        return "uncertain"
    store.resolve(entry["key"], "pending" if packet else "sent", message_id=message_id, pause_until=now + 2)
    return "card_sent" if packet else "sent"
