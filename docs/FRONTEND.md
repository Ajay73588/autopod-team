# AutoPod Cloud — Frontend Documentation

The AutoPod Cloud management dashboard is a **React 19** Single-Page Application (SPA) built with React Router DOM 7, Axios, Lucide icons, and Create React App 5 (`frontend/`).

---

## 1. Architecture & Zero-Secret Client Security

The frontend deliberately **never stores or transmits administrative secrets** in browser memory, `localStorage`, or JavaScript bundles (`frontend/src/services/api.js`):

```javascript
// Step 47B: Zero Client-Side Admin Secret Storage
// The admin API key is injected server-side by the Nginx reverse proxy upon successful
// Basic Authentication. The browser never receives, handles, or stores the admin key.
export const getAdminApiKey = () => '';
export const setAdminApiKey = () => {};
```

- When deployed behind Control Plane Nginx TLS (`:443`), the operator authenticates once using **HTTP Basic Authentication** (`/etc/nginx/.htpasswd`).
- Nginx validates Basic Auth on `/api/*` requests and injects `X-Admin-Key` upstream to the Flask backend on port `5000`.
- `API_BASE_URL` uses `process.env.REACT_APP_API_URL` (empty string `""` in production for relative `/api/...` proxying; `http://localhost:5000` in local dev).

---

## 2. Application Structure

```text
frontend/
├── Dockerfile                  # Frontend runtime container (port 3000)
├── nginx.conf                  # Static asset SPA routing config
├── package.json                # React 19, Axios, Router DOM 7 & Jest moduleNameMapper
├── public/
└── src/
    ├── App.js                  # Route map & persistent sidebar layout
    ├── services/
    │   └── api.js              # Axios client, API endpoint wrappers & interceptors
    ├── utils/
    │   └── timezone.js         # IST/local timestamp formatting helpers
    ├── components/
    │   ├── ContainerGrid.js    # Container card grid, host telemetry, safe action locking
    │   ├── ContainerActions.js # Start/Stop/Restart/Remove/Open/TAR download controls
    │   ├── DeploymentSection.js# Quick one-click deploy widget & GitHub webhook URL display
    │   ├── DeploymentResultCard.js
    │   ├── Header.js / Sidebar.js / Layout.js
    │   ├── QuickActions.js / StatCard.js / SplashScreen.js
    └── pages/
        ├── Dashboard.js        # Live stats overview, quick deploy & recent containers
        ├── ContainerManagement.js # Full fleet container table/grid & lifecycle control
        ├── ImageRegistry.js    # ECR vs Local Docker image metadata & optional Docker Hub login
        ├── Deployments.js      # Deployment history & asynchronous queue tracking
        ├── Rollback.js         # Version selector & instant ECR rollback
        ├── Fleet.js            # Registered apps, ASG worker nodes & rolling deploy UI
        ├── Logs.js             # Real-time SSE log viewer & filtered log search
        ├── Networking.js       # Container ports & Docker bridge network inspection
        └── HealthMonitoring.js # Control plane & workload health diagnostics
```

---

## 3. Key UI Workflows

### 3.1 Live Dashboard & Container Grid (`Dashboard.js`, `ContainerGrid.js`)
- **Real-Time Telemetry:** Computes overview counts (**Total**, **Running**, **Stopped**, **Issues**) dynamically directly from live `/api/containers` responses so cards and metrics remain synchronized.
- **Node & App Metadata:** Each card displays Application Name, Container Name, Execution Host (`Worker <instance-id>` vs `Control Plane`), Status & Uptime, Worker Port (`Port 808X -> 80`), `StartedAt` timestamp, Created Time, and Lifecycle State.
- **Anti-Conflict Action Guard:** While any action (`start`, `stop`, `restart`, `remove`, `download-image`) executes on a container, conflicting buttons on that card are automatically disabled until verified completion.
- **Infrastructure Safeguard:** Protected infrastructure containers (`autopod-frontend`, `autopod-backend`, `autopod-webhook-proxy`) disable deletion in the UI with explanatory tooltips and are rejected with `HTTP 403` at the backend.
- **Public URL Resolution (`Open`):** Calls `POST /api/containers/<name>/open` to open the verified AWS ALB application path (never leaking `localhost` or internal ports); internal services surface a clean informational alert (`HTTP 409`).
- **Docker Image `.tar` Export:** Triggers `POST /api/containers/<name>/download-image` to export the image to S3 and initiate a direct browser download via 15-minute presigned SigV4 URL.

### 3.2 Image Registry (`ImageRegistry.js`)
- **AWS ECR Native Clarity:** Displays an architectural banner confirming that Amazon ECR powers automated production builds while optional Docker Hub login remains available for custom external mirrors.
- **Registry Differentiation Badges:** Tags every image cleanly as **`☁️ AWS ECR`** or **`🐳 Local Docker`**, formatting repository, tag, image ID, human-readable size, and creation timestamp.

### 3.3 Deployments, Fleet & Rollback (`Deployments.js`, `Fleet.js`, `Rollback.js`)
- **Fleet Registration:** Register Git repositories, worker target ports, routing paths, and health check paths, and trigger AWS ASG worker node discovery (`POST /api/app-nodes/sync`).
- **One-Click Rollback:** Select any registered application to view recorded historical versions (`version_id`, git commit SHA, image tag, active badge) and roll back immediately without rebuilding source code.

---

## 4. Testing & Production Build

Create React App 5 uses Jest, with `package.json` configuring `moduleNameMapper` so ESM packages (`react-router-dom` v7 and `axios` v1.13) resolve cleanly in Node Jest tests:

```bash
cd frontend
npm install

# Run full Jest + React Testing Library suite (8 suites / 15 tests)
CI=true npm test -- --watchAll=false

# Verify production bundle compilation
npm run build
```
