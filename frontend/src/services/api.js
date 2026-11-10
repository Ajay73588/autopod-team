import axios from 'axios';

export const API_BASE_URL = process.env.REACT_APP_API_URL !== undefined
  ? process.env.REACT_APP_API_URL
  : (process.env.NODE_ENV === 'production' ? '' : 'http://localhost:5000');


// Step 47B: Zero Client-Side Admin Secret Storage
// The admin API key is injected server-side by the Nginx reverse proxy upon successful
// Basic Authentication. The browser never receives, handles, or stores the admin key.
export const getAdminApiKey = () => '';
export const setAdminApiKey = () => {};

// Create axios instance with consistent configuration
const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000, // 5 minutes timeout for long operations
  headers: {
    'Content-Type': 'application/json',
  },
});

// Add request interceptor for logging
apiClient.interceptors.request.use(
  (config) => {
    console.log(`🚀 Making ${config.method?.toUpperCase()} request to: ${config.url}`);
    return config;
  },
  (error) => {
    console.error('Request Error:', error);
    return Promise.reject(error);
  }
);

// Add response interceptor for error handling while preserving response status and payload
apiClient.interceptors.response.use(
  (response) => {
    console.log(`✅ Response received from: ${response.config.url}`, response.status, response.data);
    return response;
  },
  (error) => {
    console.error('❌ API Error:', {
      url: error.config?.url,
      method: error.config?.method,
      status: error.response?.status,
      message: error.message,
      data: error.response?.data
    });
    
    if (error.response) {
      const serverMessage = error.response.data?.error || error.response.data?.message || error.response.statusText || 'Server error';
      const customError = new Error(serverMessage);
      customError.response = error.response;
      customError.status = error.response.status;
      customError.code = error.code;
      throw customError;
    } else if (error.request) {
      const customError = new Error('No response from server. Check if backend is running.');
      customError.request = error.request;
      customError.code = error.code;
      throw customError;
    } else {
      throw new Error(error.message || 'Unknown error occurred');
    }
  }
);

export const apiService = {
  // Applications / fleet (multi-application + multi-node endpoints)
  registerApplication: (payload) => apiClient.post('/api/applications', payload),
  getAppNodes: () => apiClient.get('/api/app-nodes', { timeout: 15000 }),
  syncAppNodes: (asgName) => apiClient.post('/api/app-nodes/sync', asgName ? { asg_name: asgName } : {}),
  getVersionsFor: (applicationId) => apiClient.get(`/api/versions?application_id=${applicationId}`, { timeout: 15000 }),
  rollingDeploy: (applicationId, versionId) =>
    apiClient.post('/api/deployments/rolling', { application_id: applicationId, version_id: versionId }),
  rollingRollback: (applicationId, versionId) =>
    apiClient.post('/api/deployments/rolling-rollback', { application_id: applicationId, version_id: versionId }),

  // Container endpoints - FIXED: Proper response handling
  getContainers: () => 
    apiClient.get('/api/containers', { timeout: 15000 })
      .then(response => {
        // Ensure consistent response structure
        if (response.data && response.data.success !== undefined) {
          return response;
        } else {
          // If backend returns raw data, wrap it in consistent structure
          return {
            ...response,
            data: {
              success: true,
              data: response.data,
              message: 'Containers fetched successfully'
            }
          };
        }
      }),
  
  getContainerStatus: () => 
    apiClient.get('/api/status', { timeout: 10000 }),
  
  syncContainers: () => 
    apiClient.post('/api/sync', {}, { timeout: 10000 }),
  
  // Container actions
  startContainer: (containerName, targetNode = null) => 
    apiClient.post('/api/containers/start', { container_name: containerName, target_node: targetNode }, { timeout: 30000 }),
  
  stopContainer: (containerName, targetNode = null) => 
    apiClient.post('/api/containers/stop', { container_name: containerName, target_node: targetNode }, { timeout: 30000 }),
  
  restartContainer: (containerName, targetNode = null) => 
    apiClient.post('/api/containers/restart', { container_name: containerName, target_node: targetNode }, { timeout: 30000 }),
  
  removeContainer: (containerName, targetNode = null) => 
    apiClient.post('/api/containers/remove', { container_name: containerName, target_node: targetNode }, { timeout: 30000 }),
  
  // Container details and monitoring
  getAllContainersHealth: () => 
    apiClient.get('/api/containers/health', { timeout: 15000 }),

  getContainerHealth: (containerName) => 
    apiClient.get(`/api/containers/${containerName}/health`, { timeout: 15000 }),
  
  getContainerStats: (containerName) => 
    apiClient.get(`/api/containers/${containerName}/stats`, { timeout: 15000 }),
  
  getContainerWebUrl: (containerName) => 
    apiClient.get(`/api/containers/${containerName}/weburl`, { timeout: 15000 }),
  
  openContainerWeb: (containerName) => 
    apiClient.post(`/api/containers/${containerName}/open`, {}, { timeout: 15000 }),

  downloadContainerImage: (containerName) => 
    apiClient.post(`/api/containers/${encodeURIComponent(containerName)}/download-image`, {}, { timeout: 300000 }),
  
  getContainerNetwork: (containerName) => 
    apiClient.get(`/api/containers/${containerName}/network`, { timeout: 15000 }),
  
  // Manual deployment endpoint (authenticated via X-Admin-Key)
  deployApplication: (data) =>
    apiClient.post('/api/deployments', data, { timeout: 300000 }),

  // Webhooks
  triggerWebhook: (data) => 
    apiClient.post('/webhook', data, { timeout: 300000 }),

  getWebhookDeliveries: (limit = 10) =>
    apiClient.get('/api/webhook/deliveries', { params: { limit }, timeout: 15000 }),
  
  // Logs endpoints
  getLogs: () => 
    apiClient.get('/api/logs', { timeout: 15000 }),
  
  getAdvancedLogs: (params) => 
    apiClient.get('/api/logs/advanced', { params, timeout: 15000 }),
  
  exportLogs: (format = 'json', container = null) => 
    apiClient.get('/api/logs/export', { 
      params: { format, container },
      timeout: 30000 
    }),
  
  // Images endpoints
  getImages: () => 
    apiClient.get('/api/images', { timeout: 15000 }),
  
  searchImages: (query, limit = 25) => 
    apiClient.get('/api/images/search', { 
      params: { q: query, limit }, 
      timeout: 15000 
    }),
  
  pullImage: (imageName) => 
    apiClient.post('/api/images/pull', { image_name: imageName }, { timeout: 300000 }),
  
  pushImage: (pushData) => 
    apiClient.post('/api/docker/push', pushData, { timeout: 300000 }),
  
  removeImage: (imageName) => 
    apiClient.post('/api/images/remove', { image_name: imageName }, { timeout: 30000 }),
  
  getImageDetails: (imageName) => 
    apiClient.get(`/api/images/${imageName}/details`, { timeout: 15000 }),
  
  getImageHistory: (imageName) => 
    apiClient.get(`/api/images/${imageName}/history`, { timeout: 15000 }),
  
  tagImage: (sourceImage, targetImage) => 
    apiClient.post('/api/images/tag', { source_image: sourceImage, target_image: targetImage }, { timeout: 30000 }),
  
  // Network endpoints
  getAllContainersNetwork: () => 
    apiClient.get('/api/containers/network/all', { timeout: 15000 }),

  getAllNetworks: () => 
    apiClient.get('/api/containers/network/all', { timeout: 15000 }),
  
  getNetworkStats: () => 
    apiClient.get('/api/network/stats', { timeout: 15000 }),
  
  // Docker Hub Authentication endpoints
  dockerLogin: (credentials) => 
    apiClient.post('/api/docker/login', credentials, { timeout: 30000 }),
  
  dockerLogout: () => 
    apiClient.post('/api/docker/logout', {}, { timeout: 15000 }),
  
  checkDockerLogin: () => 
    apiClient.get('/api/docker/check-login', { timeout: 15000 }),
  
  // Health check - quick timeout
  healthCheck: () => 
    apiClient.get('/health', { timeout: 5000 }),
  
  // Debug endpoints
  debugContainers: () => 
    apiClient.get('/api/debug/containers', { timeout: 15000 }),

  // --- Deployments / Versions / Rollback (new) ---------------------------
  getApplications: () =>
    apiClient.get('/api/applications', { timeout: 15000 }),

  getDeployments: (applicationId = null, limit = 50) =>
    apiClient.get('/api/deployments', {
      params: { application_id: applicationId || undefined, limit },
      timeout: 15000
    }),

  getDeploymentDetail: (deploymentId) =>
    apiClient.get(`/api/deployments/${deploymentId}`, { timeout: 15000 }),

  getVersions: (applicationId = null) =>
    apiClient.get('/api/versions', {
      params: { application_id: applicationId || undefined },
      timeout: 15000
    }),

  rollbackToVersion: (versionId, applicationId = null) =>
    apiClient.post('/api/deployments/rollback', {
      version_id: versionId,
      application_id: applicationId || undefined
    }, { timeout: 120000 }),
};

// Enhanced helper function to check if backend is reachable
export const checkBackendConnection = async () => {
  try {
    const response = await apiService.healthCheck();
    return {
      connected: true,
      data: response.data
    };
  } catch (error) {
    return {
      connected: false,
      error: error.message
    };
  }
};

// Enhanced helper function to test API endpoints
export const testApiEndpoints = async () => {
  const results = {};
  
  try {
    console.log('🧪 Testing API endpoints...');
    
    // Test health endpoint
    results.health = await apiService.healthCheck();
    console.log('✅ Health check passed');
    
    // Test containers endpoint
    results.containers = await apiService.getContainers();
    console.log('✅ Containers endpoint working');
    
    // Test status endpoint
    results.status = await apiService.getContainerStatus();
    console.log('✅ Status endpoint working');
    
    return {
      success: true,
      results,
      message: 'All API endpoints are working correctly'
    };
  } catch (error) {
    console.error('❌ API test failed:', error);
    return {
      success: false,
      error: error.message,
      results
    };
  }
};

// Helper function to test container actions
export const testContainerAction = async (action, containerName) => {
  try {
    console.log(`🧪 Testing ${action} on container: ${containerName}`);
    
    let response;
    switch (action) {
      case 'start':
        response = await apiService.startContainer(containerName);
        break;
      case 'stop':
        response = await apiService.stopContainer(containerName);
        break;
      case 'restart':
        response = await apiService.restartContainer(containerName);
        break;
      case 'remove':
        response = await apiService.removeContainer(containerName);
        break;
      default:
        throw new Error(`Unknown action: ${action}`);
    }
    
    console.log(`✅ ${action} test successful:`, response.data);
    return response.data;
  } catch (error) {
    console.error(`❌ ${action} test failed:`, error);
    throw error;
  }
};

export default apiClient;