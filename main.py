import os
import re
import time
import asyncio
import aiosqlite
import hashlib
import secrets
import random
from pathlib import Path
from typing import Dict, Optional, Set, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "chat.db"

UPLOAD_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

USERNAME_RE = re.compile(r"^[A-Za-z0-9 _-]{2,25}$")
MAX_MESSAGE_CHARS = 4000
HISTORY_LIMIT = 100
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
SESSION_TTL_SECONDS = 30 * 86400

EPHEMERAL_ROOMS: Dict[str, dict] = {}

ADJECTIVES = ["Neon", "Cyber", "Ghost", "Shadow", "Phantom", "Silent", "Quantum", "Echo", "Astral", "Solar", "Cosmic", "Vortex"]
NOUNS = ["Viper", "Raven", "Falcon", "Wolf", "Specter", "Otter", "Fox", "Hawk", "Phoenix", "Cipher", "Drifter", "Pulse"]

def generate_random_alias() -> str:
    return f"{random.choice(ADJECTIVES)}{random.choice(NOUNS)}_{random.randint(100, 999)}"

def make_dm_room_id(u1: str, u2: str) -> str:
    pair = sorted([u1.lower(), u2.lower()])
    return f"dm_{pair[0]}__{pair[1]}"

def parse_dm_target(room_id: str, my_username: str) -> str:
    raw = room_id.replace("dm_", "")
    parts = raw.split("__") if "__" in raw else raw.split("_")
    my_clean = my_username.strip().lower()
    for p in parts:
        if p.lower() != my_clean:
            return p
    return parts[0]

class SecureStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            filename = Path(path).name
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

def get_conn():
    return aiosqlite.connect(DB_PATH, timeout=30.0)

async def init_db():
    async with get_conn() as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                salt TEXT NOT NULL,
                password_hash TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                room_id TEXT PRIMARY KEY,
                room_name TEXT NOT NULL,
                passkey_salt TEXT NOT NULL,
                passkey_hash TEXT NOT NULL,
                created_by TEXT,
                created_at REAL NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_rooms (
                username TEXT NOT NULL,
                room_id TEXT NOT NULL,
                joined_at REAL NOT NULL,
                PRIMARY KEY (username, room_id)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                recipient TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                UNIQUE(sender, recipient)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS room_bans (
                room_id TEXT NOT NULL,
                username TEXT NOT NULL,
                banned_by TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (room_id, username)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                recipient TEXT,
                text TEXT,
                filename TEXT,
                original_name TEXT,
                reply_to_id INTEGER DEFAULT NULL,
                reply_to_sender TEXT DEFAULT NULL,
                reply_to_text TEXT DEFAULT NULL,
                is_read INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS message_reads (
                message_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                read_at REAL NOT NULL,
                PRIMARY KEY (message_id, username)
            )
        """)
        async with conn.execute("PRAGMA table_info(messages)") as cursor:
            cols = [col[1] for col in await cursor.fetchall()]
            if "reply_to_id" not in cols:
                await conn.execute("ALTER TABLE messages ADD COLUMN reply_to_id INTEGER DEFAULT NULL")
            if "reply_to_sender" not in cols:
                await conn.execute("ALTER TABLE messages ADD COLUMN reply_to_sender TEXT DEFAULT NULL")
            if "reply_to_text" not in cols:
                await conn.execute("ALTER TABLE messages ADD COLUMN reply_to_text TEXT DEFAULT NULL")
        await conn.commit()

async def purge_expired_public_messages():
    while True:
        try:
            cutoff = time.time() - (48 * 3600)
            async with get_conn() as conn:
                async with conn.execute(
                    "SELECT filename FROM messages WHERE room_id = 'public' AND created_at < ? AND filename IS NOT NULL",
                    (cutoff,)
                ) as cursor:
                    old_files = await cursor.fetchall()
                    for (f,) in old_files:
                        try:
                            p = UPLOAD_DIR / f
                            if p.exists():
                                p.unlink()
                        except Exception:
                            pass
                await conn.execute("DELETE FROM messages WHERE room_id = 'public' AND created_at < ?", (cutoff,))
                await conn.commit()
        except Exception:
            pass
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    task = asyncio.create_task(purge_expired_public_messages())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)
app.mount("/uploads", SecureStaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

def hash_pass(password: str, salt: str = None) -> tuple:
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return salt, pwd_hash

async def create_session(username: str) -> str:
    token = secrets.token_hex(32)
    async with get_conn() as conn:
        await conn.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token, username, time.time()))
        await conn.commit()
    return token

async def get_user_by_token(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    now = time.time()
    async with get_conn() as conn:
        async with conn.execute("SELECT username, created_at FROM sessions WHERE token = ?", (token,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            username, created_at = row
            if now - created_at > SESSION_TTL_SECONDS:
                await conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                await conn.commit()
                return None
            return username

async def is_user_banned(room_id: str, username: str):
    if room_id == "public" or room_id.startswith("dm_") or room_id.startswith("tmp_"):
        return False, 0
    async with get_conn() as conn:
        async with conn.execute(
            "SELECT expires_at FROM room_bans WHERE room_id = ? AND username = ?", (room_id, username)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, 0
        expires_at = row[0]
        if expires_at == -1:
            return True, -1
        if expires_at > time.time():
            return True, expires_at
        await conn.execute("DELETE FROM room_bans WHERE room_id = ? AND username = ?", (room_id, username))
        await conn.commit()
        return False, 0

async def save_message(
    room_id: str,
    sender: str,
    text: Optional[str],
    filename: Optional[str] = None,
    original_name: Optional[str] = None,
    reply_to_id: Optional[int] = None,
    reply_to_sender: Optional[str] = None,
    reply_to_text: Optional[str] = None,
    is_read: int = 0
) -> int:
    async with get_conn() as conn:
        cursor = await conn.execute(
            """INSERT INTO messages 
               (room_id, sender, recipient, text, filename, original_name, reply_to_id, reply_to_sender, reply_to_text, is_read, created_at) 
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (room_id, sender, text, filename, original_name, reply_to_id, reply_to_sender, reply_to_text, is_read, time.time()),
        )
        await conn.commit()
        return cursor.lastrowid

async def record_read(message_id: int, username: str):
    async with get_conn() as conn:
        await conn.execute("INSERT OR IGNORE INTO message_reads (message_id, username, read_at) VALUES (?, ?, ?)",
                           (message_id, username, time.time()))
        await conn.execute("UPDATE messages SET is_read = 2 WHERE id = ?", (message_id,))
        await conn.commit()

async def get_message_readers(message_id: int) -> List[str]:
    async with get_conn() as conn:
        async with conn.execute("SELECT username FROM message_reads WHERE message_id = ? ORDER BY read_at ASC", (message_id,)) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

async def load_history(room_id: str, limit: int = HISTORY_LIMIT):
    async with get_conn() as conn:
        async with conn.execute(
            """SELECT id, sender, text, filename, original_name, reply_to_id, reply_to_sender, reply_to_text, is_read, created_at 
               FROM messages WHERE room_id = ? ORDER BY id DESC LIMIT ?""",
            (room_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
    rows.reverse()
    res = []
    for r in rows:
        res.append({
            "id": r[0],
            "sender": r[1],
            "text": r[2],
            "filename": r[3],
            "original_name": r[4],
            "reply_to": {
                "id": r[5],
                "sender": r[6],
                "text": r[7]
            } if r[5] else None,
            "is_read": r[8],
            "timestamp": r[9]
        })
    return res

class RoomConnectionManager:
    def __init__(self):
        self.rooms: Dict[str, Dict[str, tuple[str, WebSocket]]] = {}

    def get_online_users(self, include_ephemeral: bool = False) -> Set[str]:
        online = set()
        for rid, clients in self.rooms.items():
            if not include_ephemeral and rid.startswith("tmp_"):
                continue
            for uname, _ in clients.values():
                online.add(uname)
        return online

    def get_room_participants(self, room_id: str) -> List[str]:
        room_id = room_id.strip().lower()
        if room_id == "public":
            return list(self.get_online_users(include_ephemeral=False))
        if room_id in self.rooms:
            return list({uname for uname, _ in self.rooms[room_id].values()})
        return []

    async def connect(self, websocket: WebSocket, room_id: str, requested_username: str, client_id: str) -> Optional[str]:
        room_id = room_id.strip().lower()

        if room_id.startswith("tmp_"):
            if room_id not in EPHEMERAL_ROOMS:
                await websocket.close(code=4004, reason="Ephemeral room does not exist.")
                return None
            meta = EPHEMERAL_ROOMS[room_id]
            current_users = len(self.rooms.get(room_id, {}))
            if current_users >= meta["max_users"] and client_id not in self.rooms.get(room_id, {}):
                await websocket.close(code=4003, reason="Room capacity limit reached.")
                return None
        else:
            if room_id != "public":
                banned, _ = await is_user_banned(room_id, requested_username)
                if banned:
                    await websocket.close(code=4003, reason="You are banned from this room.")
                    return None

                async with get_conn() as conn:
                    async with conn.execute("SELECT 1 FROM user_rooms WHERE username = ? AND room_id = ?", (requested_username, room_id)) as cursor:
                        if not await cursor.fetchone():
                            await websocket.close(code=4003)
                            return None

        await websocket.accept()
        if room_id not in self.rooms:
            self.rooms[room_id] = {}

        actual_username = requested_username
        existing_names = {u for u, _ in self.rooms[room_id].values()}
        if room_id.startswith("tmp_") and actual_username in existing_names:
            suffix = 2
            while f"{requested_username}_{suffix}" in existing_names:
                suffix += 1
            actual_username = f"{requested_username}_{suffix}"

        self.rooms[room_id][client_id] = (actual_username, websocket)
        
        if room_id in EPHEMERAL_ROOMS:
            EPHEMERAL_ROOMS[room_id]["has_had_users"] = True

        created_by = ""
        room_name = "Public Group"
        if room_id.startswith("tmp_"):
            room_name = "⚡ Ephemeral Space"
        elif room_id.startswith("dm_"):
            target_user = parse_dm_target(room_id, actual_username)
            room_name = f"@{target_user}"
        else:
            async with get_conn() as conn:
                async with conn.execute("SELECT room_name, created_by FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
                    r = await cursor.fetchone()
                    if r:
                        room_name, created_by = r

        async with get_conn() as conn:
            async with conn.execute("SELECT id FROM messages WHERE room_id = ? AND sender != ?", (room_id, actual_username)) as cursor:
                unread_ids = await cursor.fetchall()
                for (mid,) in unread_ids:
                    await record_read(mid, actual_username)

        history = await load_history(room_id)
        room_members = self.get_room_participants(room_id)

        await websocket.send_json({
            "type": "init",
            "assigned_username": actual_username,
            "client_id": client_id,
            "is_admin": (created_by == actual_username),
            "created_by": created_by,
            "room_code": room_id,
            "room_name": room_name,
            "is_ephemeral": room_id.startswith("tmp_"),
            "messages": history,
            "members": room_members
        })
        
        await self.broadcast_payload(room_id, {"type": "all_read_ack", "reader": actual_username})
        await self.broadcast_presence_update()
        if not room_id.startswith("dm_"):
            await self.broadcast_system(room_id, f"{actual_username} connected.")
        return actual_username

    async def disconnect(self, room_id: str, client_id: str):
        room_id = room_id.strip().lower()
        if room_id in self.rooms and client_id in self.rooms[room_id]:
            username, _ = self.rooms[room_id][client_id]
            del self.rooms[room_id][client_id]
            
            if room_id in EPHEMERAL_ROOMS and len(self.rooms[room_id]) == 0 and EPHEMERAL_ROOMS[room_id]["has_had_users"]:
                del self.rooms[room_id]
                del EPHEMERAL_ROOMS[room_id]
                
                async with get_conn() as conn:
                    async with conn.execute("SELECT filename FROM messages WHERE room_id = ? AND filename IS NOT NULL", (room_id,)) as cursor:
                        files = await cursor.fetchall()
                        for (f,) in files:
                            try:
                                (UPLOAD_DIR / f).unlink(missing_ok=True)
                            except Exception:
                                pass
                    await conn.execute("DELETE FROM messages WHERE room_id = ?", (room_id,))
                    await conn.commit()
                return

            if not self.rooms[room_id]:
                del self.rooms[room_id]

            await self.broadcast_presence_update()
            if not room_id.startswith("dm_"):
                await self.broadcast_system(room_id, f"{username} disconnected.")

    async def kick_user(self, room_id: str, username: str, reason: str):
        room_id = room_id.strip().lower()
        if room_id not in self.rooms:
            return
        want = username.strip().lower()
        targets = [cid for cid, (uname, _) in self.rooms[room_id].items() if uname.lower() == want]
        for cid in targets:
            _, ws = self.rooms[room_id][cid]
            try:
                await ws.send_json({"type": "kicked", "reason": reason})
                await ws.close(code=4003)
            except Exception:
                pass
            del self.rooms[room_id][cid]
        if targets:
            await self.broadcast_presence_update()

    async def notify_user(self, username: str, payload: dict):
        want = username.strip().lower()
        for room_dict in self.rooms.values():
            for uname, ws in room_dict.values():
                if uname.lower() == want:
                    try:
                        await ws.send_json(payload)
                    except Exception:
                        pass

    async def broadcast_presence_update(self):
        for rid in list(self.rooms.keys()):
            participants = self.get_room_participants(rid)
            await self.broadcast_payload(rid, {"type": "presence_sync", "members": participants})

    async def broadcast_system(self, room_id: str, text: str):
        room_id = room_id.strip().lower()
        if room_id in self.rooms:
            payload = {"type": "system", "text": text, "timestamp": time.time()}
            for _, ws in list(self.rooms[room_id].values()):
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass

    async def broadcast_payload(self, room_id: str, payload: dict):
        room_id = room_id.strip().lower()
        if room_id not in self.rooms:
            return
        for _, ws in list(self.rooms[room_id].values()):
            try:
                await ws.send_json(payload)
            except Exception:
                pass

manager = RoomConnectionManager()

@app.get("/api/account/profile")
async def get_account_profile(token: str = Query(...)):
    user = await get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"status": "ok", "username": user}

@app.get("/api/messages/readers")
async def fetch_message_readers(message_id: int = Query(...), token: Optional[str] = Query(None)):
    readers = await get_message_readers(message_id)
    return {"message_id": message_id, "readers": readers}

@app.get("/api/rooms/details")
async def get_room_details(room_id: str = Query(...), token: Optional[str] = Query(None)):
    room_id = room_id.strip().lower()
    global_online = manager.get_online_users(include_ephemeral=False)
    in_room_online = set()
    
    if room_id in manager.rooms:
        for uname, _ in manager.rooms[room_id].values():
            in_room_online.add(uname)

    if room_id.startswith("tmp_"):
        meta = EPHEMERAL_ROOMS.get(room_id, {"max_users": 2})
        members = [{"username": u, "presence": "here"} for u in in_room_online]
        return {
            "room_id": room_id,
            "room_name": "⚡ Ephemeral Space",
            "created_by": "System (Ephemeral)",
            "max_users": meta.get("max_users", 2),
            "is_ephemeral": True,
            "is_private": True,
            "members": members,
            "total_members": len(members)
        }

    if room_id.startswith("dm_"):
        my_user = await get_user_by_token(token) or ""
        target_user = parse_dm_target(room_id, my_user)
        raw_members = room_id.replace("dm_", "").split("__" if "__" in room_id else "_")
        
        members = []
        for u in set(raw_members):
            if u in in_room_online:
                pres = "here"
            elif u in global_online:
                pres = "elsewhere"
            else:
                pres = "offline"
            members.append({"username": u, "presence": pres})

        return {
            "room_id": room_id,
            "room_name": f"@{target_user}",
            "created_by": "Direct Stream",
            "is_ephemeral": False,
            "is_private": True,
            "members": members,
            "total_members": len(members)
        }

    if room_id == "public":
        members = [{"username": u, "presence": "online"} for u in global_online]
        return {
            "room_id": "public",
            "room_name": "Public Group",
            "created_by": "Global Channel",
            "is_ephemeral": False,
            "is_private": False,
            "members": members,
            "total_members": len(members)
        }

    async with get_conn() as conn:
        async with conn.execute("SELECT room_name, created_by FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
            room = await cursor.fetchone()
            if not room:
                raise HTTPException(status_code=404, detail="Space not found.")
            rname, creator = room

        async with conn.execute("SELECT username FROM user_rooms WHERE room_id = ? ORDER BY username ASC", (room_id,)) as cursor:
            rows = await cursor.fetchall()
            all_users = [r[0] for r in rows]

    members = []
    for u in all_users:
        if u in in_room_online:
            pres = "here"
        elif u in global_online:
            pres = "elsewhere"
        else:
            pres = "offline"
        members.append({"username": u, "presence": pres})

    order = {"here": 0, "elsewhere": 1, "offline": 2}
    members.sort(key=lambda x: (order.get(x["presence"], 3), x["username"]))

    return {
        "room_id": room_id,
        "room_name": rname,
        "created_by": creator,
        "is_ephemeral": False,
        "is_private": True,
        "members": members,
        "total_members": len(members)
    }

@app.post("/api/ephemeral/create")
async def create_ephemeral(room_type: str = Form("dm"), max_limit: int = Form(2)):
    if room_type == "dm":
        limit = 2
    else:
        try:
            limit = int(max_limit)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid capacity limit format.")
        if limit < 2 or limit > 50:
            raise HTTPException(status_code=400, detail="Capacity limit must be between 2 and 50.")

    room_code = f"tmp_{secrets.token_hex(4)}"
    EPHEMERAL_ROOMS[room_code] = {"max_users": limit, "is_ephemeral": True, "has_had_users": False}
    random_alias = generate_random_alias()
    return {
        "status": "ok",
        "room_id": room_code,
        "max_users": limit,
        "room_type": room_type,
        "assigned_alias": random_alias
    }

@app.post("/api/ephemeral/verify")
async def verify_ephemeral(room_id: str = Form(...)):
    rid = room_id.strip().lower()
    if rid not in EPHEMERAL_ROOMS:
        raise HTTPException(status_code=404, detail="Ephemeral space not found or already terminated.")
    current_count = len(manager.rooms.get(rid, {}))
    meta = EPHEMERAL_ROOMS[rid]
    if current_count >= meta["max_users"]:
        raise HTTPException(status_code=403, detail="Room is full.")
    random_alias = generate_random_alias()
    return {
        "status": "ok",
        "room_id": rid,
        "current_users": current_count,
        "max_users": meta["max_users"],
        "assigned_alias": random_alias
    }

@app.post("/api/register")
async def register(username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    if not USERNAME_RE.match(username) or len(password) < 6:
        raise HTTPException(status_code=400, detail="Username 2-25 chars, password 6+ chars.")
    salt, pwd_hash = hash_pass(password)
    try:
        async with get_conn() as conn:
            await conn.execute("INSERT INTO users VALUES (?, ?, ?)", (username, salt, pwd_hash))
            await conn.commit()
        return {"status": "ok", "detail": "Account created successfully! Please sign in."}
    except Exception:
        raise HTTPException(status_code=409, detail="Username already exists.")

@app.post("/api/login")
async def login(username: str = Form(...), password: str = Form(...)):
    username = username.strip().lower()
    async with get_conn() as conn:
        async with conn.execute("SELECT salt, password_hash FROM users WHERE username = ?", (username,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    salt, stored_hash = row
    _, computed_hash = hash_pass(password, salt)
    if not secrets.compare_digest(computed_hash, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = await create_session(username)
    return {"status": "ok", "username": username, "token": token}

@app.post("/api/logout")
async def logout(token: str = Form(...)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        await conn.commit()
    return {"status": "ok"}

@app.get("/api/users/directory")
async def get_directory(token: str = Query(...)):
    me = await get_user_by_token(token)
    if not me:
        raise HTTPException(status_code=401, detail="Unauthorized")
    online_set = manager.get_online_users(include_ephemeral=False)
    async with get_conn() as conn:
        async with conn.execute("SELECT username FROM users ORDER BY username ASC") as cursor:
            rows = await cursor.fetchall()
            all_users = [r[0] for r in rows if r[0] != me]
    user_list = [{"username": u, "online": (u in online_set)} for u in all_users]
    user_list.sort(key=lambda x: (not x["online"], x["username"]))
    return {"total_online": len(online_set), "users": user_list}

@app.get("/api/users/search")
async def search_users(token: str = Query(...), q: str = Query("")):
    me = await get_user_by_token(token)
    if not me:
        raise HTTPException(status_code=401, detail="Unauthorized")
    needle = q.strip().lower().replace("%", "").replace("_", "")[:25]
    if len(needle) < 1:
        return {"users": []}
    online_set = manager.get_online_users(include_ephemeral=False)
    async with get_conn() as conn:
        async with conn.execute(
            "SELECT username FROM users WHERE username != ? AND username LIKE ? ORDER BY username ASC LIMIT 25",
            (me, f"%{needle}%"),
        ) as cursor:
            rows = await cursor.fetchall()
    users = [{"username": r[0], "online": (r[0] in online_set)} for r in rows]
    users.sort(key=lambda x: (not x["online"], x["username"]))
    return {"users": users}

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    token: Optional[str] = Form(None),
    room_id: str = Form(...),
    guest_user: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    reply_to_id: Optional[int] = Form(None),
    reply_to_sender: Optional[str] = Form(None),
    reply_to_text: Optional[str] = Form(None)
):
    room_id = room_id.strip().lower()
    
    if room_id.startswith("tmp_"):
        me = guest_user or "Anonymous"
    else:
        me = await get_user_by_token(token)
        if not me:
            raise HTTPException(status_code=401, detail="Unauthorized")
    
    orig_name = Path(file.filename).name
    ext = Path(file.filename).suffix.lower()
    safe_ext = ext if len(ext) > 1 and len(ext) <= 8 else ".bin"
    unique_name = f"{int(time.time())}_{secrets.token_hex(4)}{safe_ext}"
    save_path = UPLOAD_DIR / unique_name
    
    total_written = 0
    with open(save_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            total_written += len(chunk)
            if total_written > MAX_UPLOAD_SIZE:
                f.close()
                save_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="File limit is 100MB")
            f.write(chunk)
    
    peers = manager.rooms.get(room_id, {})
    other_peers_present = any(uname != me for uname, _ in peers.values())
    initial_read = 1 if other_peers_present else 0

    msg_id = await save_message(
        room_id=room_id,
        sender=me,
        text="",
        filename=unique_name,
        original_name=orig_name,
        reply_to_id=reply_to_id,
        reply_to_sender=reply_to_sender,
        reply_to_text=reply_to_text,
        is_read=initial_read
    )
    
    payload = {
        "type": "chat",
        "id": msg_id,
        "sender": me,
        "sender_cid": client_id,
        "text": "",
        "filename": unique_name,
        "original_name": orig_name,
        "reply_to": {
            "id": reply_to_id,
            "sender": reply_to_sender,
            "text": reply_to_text
        } if reply_to_id else None,
        "is_read": initial_read,
        "timestamp": time.time()
    }
    await manager.broadcast_payload(room_id, payload)
    return {"status": "ok", "filename": unique_name, "original_name": orig_name, "id": msg_id}

@app.post("/api/dm/request")
async def send_dm_request(target: str = Form(...), token: str = Form(...)):
    me = await get_user_by_token(token)
    target = target.strip().lower()
    if not me:
        raise HTTPException(status_code=401, detail="Authentication required.")
    if me == target:
        raise HTTPException(status_code=400, detail="Cannot chat with yourself.")

    dm_room_id = make_dm_room_id(me, target)

    async with get_conn() as conn:
        async with conn.execute("SELECT 1 FROM user_rooms WHERE username = ? AND room_id = ?", (me, dm_room_id)) as cursor:
            if await cursor.fetchone():
                return {"status": "already_connected", "room_id": dm_room_id, "room_name": f"@{target}"}

        async with conn.execute("SELECT id, sender FROM dm_requests WHERE (sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?)",
                       (me, target, target, me)) as cursor:
            existing = await cursor.fetchone()
            if existing:
                if existing[1] == me:
                    return {"status": "pending", "detail": "Permission request already sent."}
                else:
                    return {"status": "pending_incoming", "detail": f"{target} already invited you!"}

        await conn.execute("INSERT INTO dm_requests (sender, recipient, status, created_at) VALUES (?, ?, 'pending', ?)",
                       (me, target, time.time()))
        await conn.commit()

    await manager.notify_user(target, {"type": "dm_request_received", "from": me})
    return {"status": "sent", "detail": f"Request sent to @{target}."}

@app.get("/api/dm/requests")
async def get_dm_requests(token: str = Query(...)):
    me = await get_user_by_token(token)
    if not me:
        raise HTTPException(status_code=401, detail="Invalid session.")
    async with get_conn() as conn:
        async with conn.execute("SELECT id, sender, created_at FROM dm_requests WHERE recipient = ? AND status = 'pending'", (me,)) as cursor:
            rows = await cursor.fetchall()
    return [{"id": r[0], "sender": r[1], "created_at": r[2]} for r in rows]

@app.post("/api/dm/respond")
async def respond_dm_request(request_id: int = Form(...), action: str = Form(...), token: str = Form(...)):
    me = await get_user_by_token(token)
    if not me:
        raise HTTPException(status_code=401, detail="Authentication required.")

    async with get_conn() as conn:
        async with conn.execute("SELECT sender, recipient FROM dm_requests WHERE id = ?", (request_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[1] != me:
                raise HTTPException(status_code=404, detail="Request not found.")

        sender = row[0]
        await conn.execute("DELETE FROM dm_requests WHERE id = ?", (request_id,))

        if action == "accept":
            dm_room_id = make_dm_room_id(me, sender)
            await conn.execute("INSERT OR IGNORE INTO rooms VALUES (?, ?, '', '', 'system', ?)", (dm_room_id, f"@{sender}", time.time()))
            await conn.execute("INSERT OR IGNORE INTO user_rooms VALUES (?, ?, ?)", (me, dm_room_id, time.time()))
            await conn.execute("INSERT OR IGNORE INTO user_rooms VALUES (?, ?, ?)", (sender, dm_room_id, time.time()))
            await conn.commit()
            await manager.notify_user(sender, {"type": "dm_request_accepted", "by": me, "room_id": dm_room_id, "room_name": f"@{me}"})
            return {"status": "accepted", "room_id": dm_room_id, "room_name": f"@{sender}"}
        await conn.commit()
        return {"status": "rejected"}

@app.get("/api/user/rooms")
async def get_user_rooms(token: str = Query(...)):
    username = await get_user_by_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid session.")

    async with get_conn() as conn:
        async with conn.execute("""
            SELECT r.room_id, r.room_name, r.created_by 
            FROM user_rooms ur
            JOIN rooms r ON ur.room_id = r.room_id
            WHERE ur.username = ?
            ORDER BY ur.joined_at ASC
        """, (username,)) as cursor:
            rows = await cursor.fetchall()
    
    result = []
    for rid, rname, creator in rows:
        if rid.startswith("dm_"):
            target_user = parse_dm_target(rid, username)
            result.append({"id": rid, "name": f"@{target_user}", "is_dm": True, "created_by": "system"})
        else:
            result.append({"id": rid, "name": rname, "is_dm": False, "created_by": creator})
    return result

@app.post("/api/rooms/create")
async def create_room(room_name: str = Form(...), passkey: str = Form(...), token: str = Form(...)):
    username = await get_user_by_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required.")
    name = room_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Room title required.")
    if len(passkey) < 4:
        raise HTTPException(status_code=400, detail="Passkey must be 4+ characters.")

    room_code = f"hub_{secrets.token_hex(4)}"
    salt, p_hash = hash_pass(passkey)
    async with get_conn() as conn:
        await conn.execute("INSERT INTO rooms VALUES (?, ?, ?, ?, ?, ?)", (room_code, name, salt, p_hash, username, time.time()))
        await conn.execute("INSERT OR IGNORE INTO user_rooms VALUES (?, ?, ?)", (username, room_code, time.time()))
        await conn.commit()
    return {"status": "ok", "room_id": room_code, "room_name": name, "created_by": username}

@app.post("/api/rooms/join")
async def join_room(room_key: str = Form(...), passkey: str = Form(...), token: str = Form(...)):
    username = await get_user_by_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required.")
    rid = room_key.strip().lower()
    if rid == "public":
        return {"status": "ok", "room_id": "public", "room_name": "Public Group", "is_admin": False}

    banned, exp = await is_user_banned(rid, username)
    if banned:
        if exp == -1:
            raise HTTPException(status_code=403, detail="You are permanently banned from this room.")
        mins = max(1, int((exp - time.time()) / 60))
        raise HTTPException(status_code=403, detail=f"You are temporarily banned. ({mins} min remaining)")

    async with get_conn() as conn:
        async with conn.execute("SELECT room_name, passkey_salt, passkey_hash, created_by FROM rooms WHERE room_id = ?", (rid,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Invalid room key. Space not found.")
            rname, salt, stored_hash, created_by = row
            if salt and stored_hash:
                _, computed_hash = hash_pass(passkey, salt)
                if not secrets.compare_digest(computed_hash, stored_hash):
                    raise HTTPException(status_code=403, detail="Incorrect passkey.")
        await conn.execute("INSERT OR IGNORE INTO user_rooms VALUES (?, ?, ?)", (username, rid, time.time()))
        await conn.commit()

    return {"status": "ok", "room_id": rid, "room_name": rname, "is_admin": (created_by == username), "created_by": created_by}

@app.post("/api/rooms/update_passkey")
async def update_room_passkey(
    room_id: str = Form(...),
    current_passkey: str = Form(...),
    new_passkey: str = Form(...),
    token: str = Form(...)
):
    admin_user = await get_user_by_token(token)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    room_id = room_id.strip().lower()
    if len(new_passkey) < 4:
        raise HTTPException(status_code=400, detail="New passkey must be at least 4 characters.")
    
    async with get_conn() as conn:
        async with conn.execute("SELECT created_by, passkey_salt, passkey_hash FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[0] != admin_user:
                raise HTTPException(status_code=403, detail="Only the room owner can modify space passkeys.")
            creator, salt, stored_hash = row
        
        _, check_hash = hash_pass(current_passkey, salt)
        if not secrets.compare_digest(check_hash, stored_hash):
            raise HTTPException(status_code=403, detail="Incorrect current passkey.")

        new_salt, new_hash = hash_pass(new_passkey)
        await conn.execute("UPDATE rooms SET passkey_salt = ?, passkey_hash = ? WHERE room_id = ?", (new_salt, new_hash, room_id))
        await conn.commit()
    
    return {"status": "ok", "detail": "Passkey successfully rotated."}

@app.post("/api/rooms/ban")
async def ban_user(
    room_id: str = Form(...),
    token: str = Form(...),
    target_user: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
    ban_type: str = Form("temp"),
    duration: str = Form("24h"),
    duration_hours: Optional[int] = Form(None),
):
    admin_user = await get_user_by_token(token)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    room_id = room_id.strip().lower()
    if room_id == "public" or room_id.startswith("dm_") or room_id.startswith("tmp_"):
        raise HTTPException(status_code=400, detail="This space cannot be moderated with bans.")
    who = (target_user or target or "").strip().lower()
    if not who:
        raise HTTPException(status_code=400, detail="Target user required.")
    if who == admin_user:
        raise HTTPException(status_code=400, detail="Cannot ban yourself.")

    async with get_conn() as conn:
        async with conn.execute("SELECT created_by FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[0] != admin_user:
                raise HTTPException(status_code=403, detail="Only the room owner can ban members.")

        if ban_type == "perm":
            expires_at = -1
        elif duration_hours is not None:
            expires_at = time.time() + (max(1, int(duration_hours)) * 3600)
        else:
            seconds_map = {"1h": 3600, "4h": 14400, "8h": 28800, "24h": 86400, "2d": 172800, "4d": 345600, "7d": 604800}
            expires_at = time.time() + seconds_map.get(duration, 86400)

        await conn.execute("INSERT OR REPLACE INTO room_bans VALUES (?, ?, ?, ?)", (room_id, who, admin_user, expires_at))
        await conn.execute("DELETE FROM user_rooms WHERE username = ? AND room_id = ?", (who, room_id))
        await conn.commit()

    await manager.kick_user(room_id, who, "You have been banned from this room.")
    await manager.broadcast_system(room_id, f"{who} was banned by the room owner.")
    return {"status": "ok", "detail": f"@{who} has been banned."}

@app.get("/api/rooms/bans")
async def list_room_bans(room_id: str = Query(...), token: str = Query(...)):
    admin_user = await get_user_by_token(token)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    room_id = room_id.strip().lower()
    async with get_conn() as conn:
        async with conn.execute("SELECT created_by FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[0] != admin_user:
                raise HTTPException(status_code=403, detail="Only the room owner can view bans.")
        async with conn.execute(
            "SELECT username, banned_by, expires_at FROM room_bans WHERE room_id = ? ORDER BY username ASC",
            (room_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        now = time.time()
        active = []
        expired = []
        for username, banned_by, expires_at in rows:
            if expires_at != -1 and expires_at <= now:
                expired.append(username)
            else:
                active.append({
                    "username": username,
                    "banned_by": banned_by,
                    "expires_at": expires_at,
                    "permanent": expires_at == -1,
                })
        if expired:
            await conn.executemany(
                "DELETE FROM room_bans WHERE room_id = ? AND username = ?",
                [(room_id, u) for u in expired],
            )
            await conn.commit()
    return {"bans": active}

@app.post("/api/rooms/unban")
async def unban_user(
    room_id: str = Form(...),
    token: str = Form(...),
    target_user: Optional[str] = Form(None),
    target: Optional[str] = Form(None),
):
    admin_user = await get_user_by_token(token)
    if not admin_user:
        raise HTTPException(status_code=401, detail="Authentication required.")
    room_id = room_id.strip().lower()
    who = (target_user or target or "").strip().lower()
    if not who:
        raise HTTPException(status_code=400, detail="Target user required.")
    async with get_conn() as conn:
        async with conn.execute("SELECT created_by FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[0] != admin_user:
                raise HTTPException(status_code=403, detail="Only the room owner can unban members.")
        await conn.execute("DELETE FROM room_bans WHERE room_id = ? AND username = ?", (room_id, who))
        await conn.commit()
    return {"status": "ok", "username": who, "detail": f"@{who} unbanned."}

@app.post("/api/rooms/destruct")
async def destruct_room(room_id: str = Form(...), token: str = Form(...)):
    user = await get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if room_id == "public" or room_id.startswith("dm_"):
        raise HTTPException(status_code=400, detail="Cannot destruct this space.")

    async with get_conn() as conn:
        async with conn.execute("SELECT created_by FROM rooms WHERE room_id = ?", (room_id,)) as cursor:
            row = await cursor.fetchone()
            if not row or row[0] != user:
                raise HTTPException(status_code=403, detail="Only the room owner can disband this group.")

        async with conn.execute("SELECT filename FROM messages WHERE room_id = ? AND filename IS NOT NULL", (room_id,)) as cursor:
            files = await cursor.fetchall()
            for (f,) in files:
                try:
                    (UPLOAD_DIR / f).unlink(missing_ok=True)
                except Exception:
                    pass

        await conn.execute("DELETE FROM messages WHERE room_id = ?", (room_id,))
        await conn.execute("DELETE FROM user_rooms WHERE room_id = ?", (room_id,))
        await conn.execute("DELETE FROM room_bans WHERE room_id = ?", (room_id,))
        await conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
        await conn.commit()

    await manager.broadcast_payload(room_id, {
        "type": "room_destructed",
        "room_id": room_id,
        "detail": "This space has been permanently disbanded by its owner."
    })
    return {"status": "ok"}

@app.get("/")
async def get_index():
    return FileResponse(STATIC_DIR / "index.html")

@app.websocket("/ws/{room_id}/{username}")
async def ws_endpoint(websocket: WebSocket, room_id: str, username: str, token: Optional[str] = Query(None), client_id: Optional[str] = Query(None)):
    room_id = room_id.strip().lower()
    requested_username = username.strip()
    cid = client_id or secrets.token_hex(6)

    if not room_id.startswith("tmp_"):
        auth_user = await get_user_by_token(token)
        if not auth_user or auth_user.lower() != requested_username.lower():
            await websocket.close(code=4003, reason="Authentication required to join persistent spaces.")
            return

    actual_user = await manager.connect(websocket, room_id, requested_username, cid)
    if not actual_user:
        return

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "chat":
                text = str(data.get("text", ""))[:MAX_MESSAGE_CHARS].strip()
                reply_to = data.get("reply_to")
                if text:
                    peers = manager.rooms.get(room_id, {})
                    other_peers_present = any(uname != actual_user for uname, _ in peers.values())
                    initial_read = 1 if other_peers_present else 0

                    reply_id = reply_to.get("id") if reply_to else None
                    reply_sender = reply_to.get("sender") if reply_to else None
                    reply_text = reply_to.get("text") if reply_to else None

                    msg_id = await save_message(
                        room_id=room_id,
                        sender=actual_user,
                        text=text,
                        reply_to_id=reply_id,
                        reply_to_sender=reply_sender,
                        reply_to_text=reply_text,
                        is_read=initial_read
                    )

                    mentions = list(set(re.findall(r"@([A-Za-z0-9_-]+)", text)))

                    payload = {
                        "type": "chat",
                        "id": msg_id,
                        "room_id": room_id,
                        "sender": actual_user,
                        "sender_cid": cid,
                        "text": text,
                        "filename": None,
                        "original_name": None,
                        "reply_to": {
                            "id": reply_id,
                            "sender": reply_sender,
                            "text": reply_text
                        } if reply_id else None,
                        "mentions": mentions,
                        "is_read": initial_read,
                        "timestamp": time.time()
                    }
                    await manager.broadcast_payload(room_id, payload)

                    # Directly alert tagged users across all active connections
                    for tagged_name in mentions:
                        if tagged_name.lower() != actual_user.lower():
                            await manager.notify_user(tagged_name, {
                                "type": "user_mentioned",
                                "by": actual_user,
                                "room_id": room_id,
                                "text": text
                            })

            elif msg_type == "typing":
                await manager.broadcast_payload(room_id, {"type": "typing", "sender": actual_user, "sender_cid": cid})

            elif msg_type == "read_ack":
                msg_id = data.get("message_id")
                if msg_id:
                    await record_read(msg_id, actual_user)
                    await manager.broadcast_payload(room_id, {
                        "type": "read_ack",
                        "message_id": msg_id,
                        "reader": actual_user
                    })

    except WebSocketDisconnect:
        await manager.disconnect(room_id, cid)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
