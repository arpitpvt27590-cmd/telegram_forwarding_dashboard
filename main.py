import os, json, asyncio, secrets, hmac, hashlib, re
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

DATA = Path(os.getenv('DATA_DIR', '/data'))
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA / 'config.json'
SESSION_DIR = DATA / 'sessions'; SESSION_DIR.mkdir(exist_ok=True)
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
API_ID = os.getenv('TG_API_ID', '')
API_HASH = os.getenv('TG_API_HASH', '')
SESSION_SECRET = os.getenv('SESSION_SECRET', secrets.token_hex(32))
app = FastAPI(title='Telegram Forwarding Dashboard')
app.mount('/static', StaticFiles(directory='static'), name='static')
templates = Jinja2Templates(directory='templates')
clients: dict[str, TelegramClient] = {}
login_flows: dict[str, dict] = {}
config = {'accounts': [], 'rules': [], 'settings': {'ignore_bots': True, 'blocked_domains': [], 'blocked_words': [], 'replacements': []}}

def load_config():
    global config
    if CONFIG_FILE.exists():
        try: config.update(json.loads(CONFIG_FILE.read_text()))
        except Exception: pass

def save_config():
    tmp = CONFIG_FILE.with_suffix('.tmp'); tmp.write_text(json.dumps(config, indent=2)); tmp.replace(CONFIG_FILE)

def token_for(password):
    return hmac.new(SESSION_SECRET.encode(), password.encode(), hashlib.sha256).hexdigest()

def authorized(request: Request):
    token = request.cookies.get('tg_session', '')
    return bool(token and hmac.compare_digest(token, token_for(ADMIN_PASSWORD)))

def require_auth(request: Request):
    if not ADMIN_PASSWORD: raise HTTPException(503, 'Set ADMIN_PASSWORD in Render environment variables first.')
    if not authorized(request): raise HTTPException(401, 'Please sign in again.')

class LoginBody(BaseModel): password: str
class PhoneBody(BaseModel): phone: str
class CodeBody(BaseModel): phone: str; code: str; password: Optional[str] = None
class RuleBody(BaseModel): account: str; source: str; destination: str; history: bool = False; enabled: bool = True; label: str = ''
class SettingsBody(BaseModel): ignore_bots: bool = True; blocked_domains: list[str] = []; blocked_words: list[str] = []; replacements: list[dict] = []

async def get_client(phone: str):
    if phone not in clients:
        if not API_ID or not API_HASH: raise HTTPException(503, 'Set TG_API_ID and TG_API_HASH in Render environment variables.')
        c = TelegramClient(str(SESSION_DIR / re.sub(r'[^0-9+]', '', phone)), int(API_ID), API_HASH)
        await c.connect(); clients[phone] = c
    return clients[phone]

def account_name(phone):
    return next((a.get('name', phone) for a in config['accounts'] if a.get('phone') == phone), phone)

def filter_message(message):
    text = (message.message or '')
    settings = config.get('settings', {})
    if settings.get('ignore_bots') and getattr(getattr(message, 'sender', None), 'bot', False): return False
    low = text.lower()
    if any(w and w.lower() in low for w in settings.get('blocked_words', [])): return False
    if any(d and d.lower() in low for d in settings.get('blocked_domains', [])): return False
    return True

def replace_links(text):
    for item in config.get('settings', {}).get('replacements', []):
        old, new = item.get('from', ''), item.get('to', '')
        if old: text = text.replace(old, new)
    return text

async def ensure_handlers(phone):
    c = clients.get(phone)
    if not c: return
    # Remove old handler before rebuilding to avoid duplicate forwards.
    old = getattr(c, '_tg_forward_handler', None)
    if old: c.remove_event_handler(old)
    async def handler(event):
        msg = event.message
        for rule in list(config.get('rules', [])):
            if not rule.get('enabled', True) or rule.get('account') != phone: continue
            try: src = int(rule['source']); dst = int(rule['destination'])
            except Exception: continue
            if event.chat_id != src or not filter_message(msg): continue
            try:
                txt = replace_links(msg.message or '')
                if msg.media:
                    await c.send_file(dst, msg.media, caption=txt or None, formatting_entities=msg.entities)
                elif txt:
                    await c.send_message(dst, txt, formatting_entities=msg.entities)
            except Exception as e: print('Forward failed:', repr(e), flush=True)
    c.add_event_handler(handler, events.NewMessage(incoming=True))
    c._tg_forward_handler = handler

@app.on_event('startup')
async def startup():
    load_config()
    # Reconnect previously authorized sessions after process restart.
    for account in config.get('accounts', []):
        phone = account.get('phone')
        if not phone: continue
        try:
            c = await get_client(phone)
            if await c.is_user_authorized(): await ensure_handlers(phone)
        except Exception as e: print('Session reconnect failed for', phone, repr(e), flush=True)

@app.get('/', response_class=HTMLResponse)
async def home(request: Request): return templates.TemplateResponse('index.html', {'request': request})
@app.get('/api/status')
async def status(request: Request):
    return {'authenticated': authorized(request), 'configured': bool(ADMIN_PASSWORD), 'api_configured': bool(API_ID and API_HASH)}
@app.post('/api/login')
async def login(body: LoginBody, response: Response):
    if not ADMIN_PASSWORD: raise HTTPException(503, 'Set ADMIN_PASSWORD in environment variables.')
    if not hmac.compare_digest(body.password, ADMIN_PASSWORD): raise HTTPException(401, 'Incorrect dashboard password.')
    response.set_cookie('tg_session', token_for(ADMIN_PASSWORD), httponly=True, secure=True, samesite='lax', max_age=86400*14)
    return {'ok': True}
@app.post('/api/logout')
async def logout(response: Response): response.delete_cookie('tg_session'); return {'ok': True}
@app.get('/api/data')
async def data(request: Request):
    require_auth(request)
    return {'accounts': [{**a, 'authorized': bool(a.get('phone') in clients and await clients[a['phone']].is_user_authorized())} for a in config['accounts']], 'rules': config['rules'], 'settings': config['settings'], 'api_configured': bool(API_ID and API_HASH)}
@app.post('/api/telegram/request-code')
async def request_code(body: PhoneBody, request: Request):
    require_auth(request)
    phone = body.phone.strip()
    c = await get_client(phone)
    sent = await c.send_code_request(phone)
    login_flows[phone] = {'phone_code_hash': sent.phone_code_hash}
    return {'ok': True, 'message': 'Telegram sent a login code. Check Telegram app/SMS; enter it here.'}
@app.post('/api/telegram/verify-code')
async def verify_code(body: CodeBody, request: Request):
    require_auth(request); phone = body.phone.strip(); flow = login_flows.get(phone)
    if not flow: raise HTTPException(400, 'Request a login code first.')
    c = await get_client(phone)
    try: await c.sign_in(phone=phone, code=body.code.strip(), phone_code_hash=flow['phone_code_hash'])
    except SessionPasswordNeededError:
        if not body.password: return {'needs_password': True, 'message': 'This account has Telegram two-step verification enabled. Enter its Telegram password.'}
        await c.sign_in(password=body.password)
    me = await c.get_me(); title = ' '.join(filter(None, [getattr(me, 'first_name', ''), getattr(me, 'last_name', '')])) or phone
    if not any(a.get('phone') == phone for a in config['accounts']): config['accounts'].append({'phone': phone, 'name': title})
    save_config(); login_flows.pop(phone, None); await ensure_handlers(phone)
    return {'ok': True, 'name': title}
@app.get('/api/chats/{phone}')
async def chats(phone: str, request: Request):
    require_auth(request)
    c = clients.get(phone)
    if not c or not await c.is_user_authorized(): raise HTTPException(400, 'Connect this Telegram account first.')
    result=[]
    async for d in c.iter_dialogs(limit=500):
        result.append({'id': str(d.id), 'title': d.name, 'type': 'channel' if d.is_channel else ('group' if d.is_group else 'private')})
    return {'chats': result}
@app.post('/api/rules')
async def add_rule(body: RuleBody, request: Request):
    require_auth(request)
    if not any(a.get('phone') == body.account for a in config['accounts']): raise HTTPException(400, 'Unknown account.')
    rule = body.model_dump(); rule['id'] = secrets.token_hex(5)
    config['rules'].append(rule); save_config(); await ensure_handlers(body.account)
    if body.history:
        asyncio.create_task(forward_history(rule))
    return {'ok': True, 'rule': rule, 'history_started': body.history}
async def forward_history(rule):
    phone = rule['account']; c = clients.get(phone)
    if not c: return
    try:
        async for msg in c.iter_messages(int(rule['source']), reverse=True, limit=1000):
            if not filter_message(msg): continue
            txt = replace_links(msg.message or '')
            if msg.media: await c.send_file(int(rule['destination']), msg.media, caption=txt or None, formatting_entities=msg.entities)
            elif txt: await c.send_message(int(rule['destination']), txt, formatting_entities=msg.entities)
            await asyncio.sleep(0.4)
    except Exception as e: print('History forwarding failed:', repr(e), flush=True)
@app.patch('/api/rules/{rule_id}')
async def toggle_rule(rule_id: str, request: Request):
    require_auth(request)
    rule = next((r for r in config['rules'] if r.get('id') == rule_id), None)
    if not rule: raise HTTPException(404, 'Rule not found.')
    rule['enabled'] = not rule.get('enabled', True); save_config(); await ensure_handlers(rule['account']); return {'ok': True, 'enabled': rule['enabled']}
@app.delete('/api/rules/{rule_id}')
async def delete_rule(rule_id: str, request: Request):
    require_auth(request); old = config['rules']; config['rules'] = [r for r in old if r.get('id') != rule_id]
    if len(old) == len(config['rules']): raise HTTPException(404, 'Rule not found.')
    save_config()
    for a in config['accounts']: await ensure_handlers(a['phone'])
    return {'ok': True}
@app.put('/api/settings')
async def set_settings(body: SettingsBody, request: Request):
    require_auth(request); config['settings'] = body.model_dump(); save_config(); return {'ok': True}
@app.delete('/api/accounts/{phone}')
async def remove_account(phone: str, request: Request):
    require_auth(request)
    c=clients.pop(phone, None)
    if c: await c.disconnect()
    config['accounts']=[a for a in config['accounts'] if a.get('phone') != phone]
    config['rules']=[r for r in config['rules'] if r.get('account') != phone]
    save_config(); return {'ok': True}
