
### 1. The Technology Stack

* **Reverse Proxy:** **Caddy** (Handles automatic SSL via Let's Encrypt, incredibly low memory footprint).
* **Backend:** **Python (FastAPI)** or **Node.js (Express/Fastify)**. FastAPI is excellent here for async streaming.
* **Database:** **SQLite**. Your data model is purely metadata (links, tokens, sessions), which will take mere megabytes. No need to waste RAM on Postgres/MySQL.
* **Frontend UI:** Vanilla JS or lightweight React/Preact, utilizing **Uppy.js** for the frontend upload queues, progress bars, and resumable uploads.
* **Storage API:** Google Drive API v3 (Resumable Uploads).

---

### 2. Data Model (SQLite Schema)

You will need four primary tables to handle the complex onboarding and session logic.

1. **Dropboxes (The Share Links)**
* `id`: UUID (acts as the custom path: `[example.com/drop/uuid](https://example.com/drop/uuid)`)
* `gdrive_folder_id`: Target destination in Google Drive
* `name`: Human-readable name
* `password_hash`: Optional bcrypt hash
* `expires_at`: Timestamp (e.g., 30 days out)
* `onboard_window_hours`: Int (e.g., 48)
* `max_devices`: Int (e.g., 3)
* `is_revoked`: Boolean


2. **Device_Sessions (The Token System)**
* `id`: UUID (Stored in a secure, HTTP-only cookie on the client's browser)
* `dropbox_id`: Foreign Key
* `created_at`: Timestamp
* `is_revoked`: Boolean


3. **Uploaded_Files**
* `id`: UUID
* `dropbox_id`: Foreign Key
* `session_id`: Foreign Key (Ties the file exclusively to the uploader's browser)
* `gdrive_file_id`: The actual ID in Google Drive
* `original_filename`: String
* `status`: Uploading / Completed


4. **Admins**
* `google_id`: Google OAuth sub ID
* `email`: Allowed admin email



---

### 3. Core Workflows & Logic

#### Authentication & Admin Interface

* **GCP Setup:** Create an OAuth 2.0 Client ID in Google Cloud. Configure the authorized redirect URI to `[https://your-domain.com/auth/google/callback](https://your-domain.com/auth/google/callback)`.
* **Admin Login:** The backend intercepts requests to `/admin/*`. If no admin session exists, redirect to Google Auth. Validate that the returned email matches your authorized admin list.
* **Share Creation:** Admin selects a GDrive folder (backend queries GDrive API for folders). Admin sets max devices, onboarding window, and expiration.
* **Helper Text:** Upon creation, the backend returns a formatted string:
```text
Hey! Please upload your files securely here: https://[domain]/drop/[id]
* Password: [if set]
* Note: This link expires in 30 days, but you must open it on your devices within 48 hours. Max 3 devices.

```



#### The Token & Device Onboarding Window

When a user visits `https://[domain]/drop/[dropbox_id]`:

1. Backend checks if the user has a valid `Device_Session` cookie for this dropbox. If yes, grant access.
2. If no cookie:
* Check if `dropbox.is_revoked` or past `expires_at`.
* Check if the current time is within `created_at + onboard_window_hours`.
* Count existing `Device_Sessions` for this dropbox. If `< max_devices`, generate a new `Device_Session` UUID, set it as an HTTP-only cookie, save to DB, and grant access.
* If outside the window or max devices reached, return 403 Forbidden.



#### Blind Upload & Session Management

* **Upload:** User drags files into Uppy.js. The backend receives the multipart stream. Instead of writing to disk, it pipes the stream directly to the Google Drive API using a Resumable Upload.
* **Sidecar Comments:** Uppy.js allows sending form fields alongside files. If a user adds a comment, the backend creates a simple JSON or TXT file (e.g., `filename.ext.meta.txt`) and uploads it to the same GDrive folder.
* **Visibility:** The frontend calls `GET /api/my-files`. The backend queries the `Uploaded_Files` table `WHERE session_id = [user's cookie]`. It only returns metadata (filename, ID).
* **Manage (Rename/Delete):**
* User clicks rename. Frontend sends `PATCH /api/files/[id]`. Backend verifies the `session_id` owns the file, then calls GDrive API to rename it.
* User clicks delete. Frontend sends `DELETE /api/files/[id]`. Backend verifies ownership, calls GDrive API to trash it, and deletes the DB row.


* **No Downloads:** There is simply no API route created that requests file content from GDrive.

---

### 4. Docker Infrastructure Recipe

On your Docker host, create a directory with a `docker-compose.yml` file. This assumes you will write the application code in a `./app` folder.

```yaml
version: '3.8'

services:
  caddy:
    image: caddy:2
    container_name: caddy_proxy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - backend

  backend:
    build: ./app
    container_name: dropbox_backend
    restart: unless-stopped
    environment:
      - GOOGLE_CLIENT_ID=your_gcp_client_id
      - GOOGLE_CLIENT_SECRET=your_gcp_client_secret
      - BASE_URL=https://your-domain.com
      - DB_PATH=/data/app.db
      - SESSION_SECRET=super_secret_random_string
    volumes:
      - ./data:/data # Holds the SQLite database ensuring it persists across container restarts

volumes:
  caddy_data:
  caddy_config:

```

**Caddyfile (`./Caddyfile`)**

```caddyfile
your-domain.com {
    reverse_proxy backend:8000
}

```

### 5. Implementation Strategy for the 20GB Disk Limit

Because your host only has 20GB of disk space, handling 500MB+ concurrent uploads will crash the server if buffered to disk.

**If using Python (FastAPI):**
You must read the incoming request as a stream and feed it directly into the Google Drive API's `MediaIoBaseUpload` in chunks.

**If using Node.js (Express):**
Use the `busboy` library to parse the incoming multipart request stream. When a file stream starts, immediately pipe it into the `@googleapis/drive` client.

```javascript
// Node.js Conceptual Example
const Busboy = require('busboy');
const { google } = require('googleapis');

req.pipe(new Busboy({ headers: req.headers })).on('file', (name, fileStream, info) => {
    drive.files.create({
        requestBody: { name: info.filename, parents: [targetFolderId] },
        media: { mimeType: info.mimeType, body: fileStream }
    });
});

```

### 6. Development Path

To build this, the steps would be:

1. Initialize a Node.js or Python project. Set up SQLite and the table schemas.
2. Implement Google OAuth2 login specifically for your admin route.
3. Build the Admin dashboard (HTML/JS) to create and list entries in the `Dropboxes` table.
4. Implement the public link route with the Device Token validation logic (setting the cookie).
5. Build the public UI utilizing Uppy.js for the upload queue and progress bars.
6. Connect Uppy's upload endpoint to the streaming GDrive upload function.
7. Wrap in a Dockerfile, spin up the Docker Compose stack on your host, and point your DNS to it.