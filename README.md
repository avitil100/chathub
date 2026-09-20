# ChatHub

> A real-time chat application and multi-room messaging platform built with **FastAPI, WebSockets, SQLite, and a responsive HTML/CSS/JavaScript frontend**.

ChatHub is a semester 5 project focused on real-time communication, private spaces, disposable rooms, user presence, file sharing, notifications, and room moderation.

## ✨ Features

### 🔐 Authentication
- User registration and sign-in
- Username validation
- Password hashing using PBKDF2-HMAC-SHA256 with per-user salts
- Session tokens with a 30-day lifetime
- Logout support

### 💬 Real-Time Messaging
- Real-time chat powered by **WebSockets**
- Persistent message history stored in SQLite
- Up to 100 recent messages loaded when joining a room
- Online presence tracking
- Join/leave system messages
- Typing indicators
- Message read acknowledgements
- View users who have read a message

### 👥 Public, Private & Direct Spaces
- Built-in **Public Group**
- Create private rooms protected by a room key and passkey
- Join existing rooms using their room key and passkey
- Direct-message requests between registered users
- Accept or reject DM requests
- Persistent personal room list
- User directory and username search

### ⚡ Disposable Rooms
ChatHub supports temporary rooms that do not require an account.

- 1-to-1 disposable DM rooms
- Disposable group rooms
- Capacity limits from 2 to 50 users for group rooms
- Automatically generated temporary aliases
- Room codes for sharing with other users
- Ephemeral rooms are removed when the last users leave
- Associated messages and uploaded files are also cleaned up

### 📎 File Sharing
- Upload files directly inside chat rooms
- Maximum upload size: **100 MB**
- Unique server-side filenames
- Original filenames are preserved for display
- Image previews
- Audio playback
- Downloadable file attachments
- Attachments can also be sent as replies

### ↩️ Replies & Mentions
- Reply to individual messages
- Inline reply preview in message bubbles
- `@username` mentions
- Mention autocomplete
- Mention highlighting
- Cross-room mention notifications

### 🔔 Notifications
- Direct-message request notifications
- Mention notifications
- System/channel activity notifications
- Notification filtering
- Notification badges
- Optional notification sound

### 🛡️ Room Moderation
Room owners can:
- Kick users
- Temporarily ban users
- Permanently ban users
- View active bans
- Unban users
- Change the room passkey
- Permanently disband a private room

### 🎨 Responsive Interface
- Dark-themed UI
- Responsive desktop/mobile layout
- Sidebar navigation
- Room information panel
- User presence indicators
- Message animations
- Toast notifications
- Mobile side panels
- Keyboard-friendly message composer

---

## 🧰 Technology Stack

### Backend
- **Python**
- **FastAPI**
- **WebSockets**
- **Uvicorn**
- **SQLite**
- **aiosqlite**

### Frontend
- **HTML5**
- **CSS3**
- **JavaScript**
- Inter font
- JetBrains Mono

### Storage
ChatHub uses SQLite for:
- User accounts
- Sessions
- Rooms
- Room memberships
- Direct-message requests
- Room bans
- Messages
- Message read records

Uploaded files are stored in the local `uploads/` directory.

---

## 📁 Project Structure

```text
chathub/
│
├── main.py                 # FastAPI backend and WebSocket server
│
├── static/
│   └── index.html          # Frontend application
│
├── uploads/                # Uploaded chat files (created automatically)
│
├── chat.db                 # SQLite database (created automatically)
│
└── README.md
```

> `uploads/` and `chat.db` are created automatically when the application starts.

---

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/avitil00/chathub.git
cd chathub
```

Replace the repository URL above if your GitHub repository uses a different owner/name.

### 2. Create a virtual environment

#### Windows

```bash
python -m venv venv
venv\Scripts\activate
```

#### Linux / macOS

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install fastapi uvicorn aiosqlite python-multipart
```

### 4. Start the server

```bash
python main.py
```

The server runs on:

```text
http://localhost:8000
```

Open that address in your browser.

### Alternative: run with Uvicorn

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## 🔄 How It Works

The application uses a FastAPI backend to provide HTTP API endpoints for authentication, room management, users, uploads, and moderation.

WebSockets handle the live communication layer:

```text
Browser
   │
   ├── HTTP API ────────────────► FastAPI
   │                                │
   │                                ├── SQLite
   │                                └── File Storage
   │
   └── WebSocket ───────────────► RoomConnectionManager
                                    │
                                    ├── Presence
                                    ├── Messages
                                    ├── Typing Events
                                    ├── Read Receipts
                                    └── Notifications
```

When a user enters a persistent room, the backend verifies their session and room membership before accepting the WebSocket connection.

Disposable rooms use a temporary in-memory room registry and are destroyed after the room becomes empty.

---

## 🔑 Important Limits

| Item | Limit |
|---|---:|
| Username length | 2–25 characters |
| Minimum password length | 6 characters |
| Minimum room passkey | 4 characters |
| Message length | 4,000 characters |
| File upload size | 100 MB |
| Message history loaded | 100 messages |
| Session lifetime | 30 days |
| Ephemeral group capacity | 2–50 users |
| Public message retention | 48 hours |

---

## 🔒 Security Measures

The current implementation includes several security-related measures:

- Passwords are not stored directly; PBKDF2-HMAC-SHA256 hashes and salts are stored instead.
- Session tokens are generated using cryptographically secure random values.
- Password comparisons use `secrets.compare_digest`.
- Room passkeys are stored as salted password hashes.
- Uploaded filenames are replaced with unique server-generated filenames.
- Upload size is checked while the file is being written.
- Persistent WebSocket rooms require an authenticated session.
- Room membership is checked before joining protected rooms.
- Room bans are checked before allowing access.

### Important deployment note

This project is intended as a student/project deployment. Before exposing it publicly, consider adding production protections such as HTTPS/WSS, secure cookies or stronger token handling, rate limiting, CSRF protection where applicable, stricter upload-type validation, reverse-proxy configuration, and production-grade database/storage management.

---

## 🗄️ Database

The SQLite database is automatically initialized when the FastAPI application starts.

Main tables include:

- `users`
- `sessions`
- `rooms`
- `user_rooms`
- `dm_requests`
- `room_bans`
- `messages`
- `message_reads`

SQLite WAL mode and a busy timeout are enabled to improve concurrent database access.

---

## 🌐 Main API Areas

The backend exposes API routes for:

```text
/api/register
/api/login
/api/logout

/api/account/profile

/api/users/directory
/api/users/search

/api/dm/request
/api/dm/requests
/api/dm/respond

/api/user/rooms

/api/rooms/create
/api/rooms/join
/api/rooms/update_passkey
/api/rooms/details
/api/rooms/ban
/api/rooms/bans
/api/rooms/unban
/api/rooms/destruct

/api/ephemeral/create
/api/ephemeral/verify

/api/upload
/api/messages/readers
```

Real-time communication is provided through:

```text
/ws/{room_id}/{username}
```

---

## 🧪 Running the Project

After starting the server:

1. Open `http://localhost:8000`.
2. Create an account using **Register**.
3. Sign in.
4. Enter the **Public Group** or create a private room.
5. Share the room key and passkey with another registered user.
6. Test real-time messaging, typing indicators, replies, mentions, and file sharing.
7. Try **Quick Disposable Room** to test temporary conversations.

---

## 📌 Project Status

This project is a semester 5 academic project demonstrating:

- Full-stack web development
- REST API development
- WebSocket-based real-time communication
- Authentication and session management
- Database design
- File handling
- User presence
- Private communication
- Room-based access control
- Basic moderation functionality
- Responsive frontend development

---

## 👨‍💻 Author

**Avitil00**

GitHub: `https://github.com/avitil00`

---

## 📄 License

This project is licensed under the MIT License.
Copyright (c) 2026 Avitil00
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files, to deal in the Software
without restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies of the
Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
