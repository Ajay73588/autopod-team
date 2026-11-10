import React, { useState, useEffect, useRef } from 'react';
import { apiService } from '../services/api';
import DeploymentResultCard from './DeploymentResultCard';
import '../styles/DeploymentSection.css';

const DeploymentSection = () => {
  const [githubUrl, setGithubUrl] = useState('');
  const [branch, setBranch] = useState('main');
  const [appName, setAppName] = useState('');
  const [statusMessage, setStatusMessage] = useState('');
  const [deploying, setDeploying] = useState(false);
  const [deploymentProgress, setDeploymentProgress] = useState('');
  const [deploymentResult, setDeploymentResult] = useState(null);
  const [webhookDeliveries, setWebhookDeliveries] = useState(null);
  const [verifyingWebhook, setVerifyingWebhook] = useState(false);
  const [copiedUrl, setCopiedUrl] = useState(false);
  const pollingRef = useRef(null);

  const stopPolling = () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }
  };

  useEffect(() => {
    return () => stopPolling();
  }, []);

  const sanitizeAppName = (name) => {
    if (!name) return '';
    return name
      .toLowerCase()
      .trim()
      .replace(/[^a-z0-9-]/g, '-')
      .replace(/-+/g, '-')
      .replace(/^-+|-+$/g, '');
  };

  const getRepoNameFromUrl = (url) => {
    try {
      const match = url.match(/github\.com\/([^/]+\/[^/]+?)(?:\.git)?$/);
      const raw = match ? match[1].split('/').pop().replace('.git', '') : 'github-app';
      return sanitizeAppName(raw) || 'github-app';
    } catch {
      return 'github-app';
    }
  };

  const handleUrlChange = (url) => {
    setGithubUrl(url);
    if (url && !appName) {
      setAppName(getRepoNameFromUrl(url));
    }
  };

  const triggerDeployment = async (overrideUrl = null, overrideName = null, overrideBranch = null) => {
    const targetUrl = (overrideUrl !== null ? overrideUrl : githubUrl).trim();
    const targetName = (overrideName !== null ? overrideName : appName).trim() || getRepoNameFromUrl(targetUrl);
    const targetBranch = (overrideBranch !== null ? overrideBranch : branch).trim() || 'main';

    if (!targetUrl) {
      setStatusMessage('❌ Please enter a GitHub repository URL');
      return;
    }

    stopPolling();

    try {
      setDeploying(true);
      setStatusMessage('🚀 Starting deployment process...');
      setDeploymentProgress('Initializing deployment and building container...');

      const deploymentData = {
        repository: {
          clone_url: targetUrl,
          name: targetName
        },
        branch: targetBranch
      };

      console.log('Sending deployment request:', deploymentData);

      // Start polling for real-time progress & completion
      let pollCount = 0;
      const maxPolls = 100; // ~5 minutes of polling every 3 seconds
      pollingRef.current = setInterval(async () => {
        pollCount++;
        if (pollCount > maxPolls) {
          stopPolling();
          setDeploying(false);
          setStatusMessage('⏳ Deployment is still running in background. Please check the Applications or Container Management tab.');
          return;
        }

        try {
          const depRes = await apiService.getDeployments(null, 5);
          if (depRes.data?.success && Array.isArray(depRes.data?.data) && depRes.data.data.length > 0) {
            const latestDep = depRes.data.data[0];
            if (latestDep.status === 'in_progress' || latestDep.status === 'building' || latestDep.status === 'deploying') {
              setDeploymentProgress(`Deployment #${latestDep.id} in progress (${latestDep.status})...`);
            } else if (latestDep.status === 'success') {
              stopPolling();
              setDeploying(false);
              setStatusMessage(`✅ Deployment #${latestDep.id} completed successfully!`);
              setDeploymentProgress('Deployment completed!');

              // Find container / app info for public_url
              try {
                const contRes = await apiService.getContainers();
                const conts = contRes.data?.data?.containers || contRes.data?.data || [];
                const matched = conts.find(c =>
                  (c.name && c.name.includes(targetName)) ||
                  (c.container_name && c.container_name.includes(targetName))
                );
                const pubUrl = matched?.public_url || latestDep.public_url;
                const port = matched?.port || latestDep.port;
                const cName = matched?.name || matched?.container_name || targetName;
                setDeploymentResult({
                  repo_url: targetUrl,
                  repo_name: targetName,
                  container_name: cName,
                  port: port || 'Auto',
                  public_url: pubUrl
                });
              } catch (_) {
                setDeploymentResult({
                  repo_url: targetUrl,
                  repo_name: targetName,
                  container_name: targetName,
                  public_url: latestDep.public_url
                });
              }
            } else if (latestDep.status === 'failed') {
              stopPolling();
              setDeploying(false);
              setStatusMessage(`❌ Deployment #${latestDep.id} failed: ${latestDep.error_message || 'Deployment failed'}`);
              setDeploymentProgress('Deployment failed - check backend logs');
            }
          }
        } catch (pollErr) {
          console.warn('Polling error:', pollErr);
        }
      }, 3000);

      // Call manual deployment endpoint
      const response = await apiService.deployApplication(deploymentData);
      console.log('Deployment response received:', response);

      if (response.data && response.data.status === 'success') {
        stopPolling();
        const successMessage = response.data.message || 'Deployment completed successfully!';
        setStatusMessage(`✅ ${successMessage}`);
        setDeploymentProgress('Deployment completed!');
        
        if (response.data.data) {
          const resultData = response.data.data;
          setDeploymentResult({
            ...resultData,
            repo_url: targetUrl,
            repo_name: targetName
          });
          const { container_name, port, public_url } = resultData;
          if (public_url) {
            setDeploymentProgress(`Container: ${container_name} | Port: ${port} | Public URL: ${public_url}`);
          } else {
            setDeploymentProgress(`Container: ${container_name} | Port: ${port} (Worker deployment active)`);
          }
        }
        
        setGithubUrl('');
        setAppName('');
        setDeploying(false);
      } else {
        const errorMsg = response.data?.message || response.data?.error || 'Unknown error occurred';
        setStatusMessage(`❌ Deployment failed: ${errorMsg}`);
        setDeploymentProgress('Deployment failed - check backend logs');
        stopPolling();
        setDeploying(false);
      }
    } catch (error) {
      console.error('Deployment error details:', error);
      
      if (error.code === 'ECONNABORTED') {
        setStatusMessage('⏳ Build/deploy is taking longer than usual. Monitoring background progress...');
        setDeploymentProgress('Building container image... Monitoring deployment progress.');
        // Do NOT stop polling; let the background poller detect completion!
      } else if (error.status === 401 || error.response?.status === 401) {
        stopPolling();
        setDeploying(false);
        setStatusMessage('❌ Authentication failed: Invalid or missing Admin API Key');
        setDeploymentProgress('Please check your authentication credentials');
      } else if (error.response) {
        // Give background poller 2 seconds in case it was a proxy timeout but build succeeded
        setTimeout(() => {
          if (!deploymentResult) {
            stopPolling();
            setDeploying(false);
            const serverError = error.response.data?.message || error.response.data?.error || error.response.statusText;
            setStatusMessage(`❌ Server error: ${serverError}`);
            setDeploymentProgress('Server returned an error');
          }
        }, 3000);
      } else if (error.request) {
        setTimeout(() => {
          if (!deploymentResult) {
            stopPolling();
            setDeploying(false);
            setStatusMessage('❌ No response from server. Check if backend is running on port 5000.');
            setDeploymentProgress('Connection failed - backend might be down');
          }
        }, 3000);
      } else {
        stopPolling();
        setDeploying(false);
        setStatusMessage(`❌ Deployment error: ${error.message}`);
        setDeploymentProgress('Deployment failed');
      }
    }
  };

  const triggerSampleDeployment = (sampleUrl, sampleContainerName) => {
    const cleanName = sanitizeAppName(sampleContainerName);
    setGithubUrl(sampleUrl);
    setAppName(cleanName);
    triggerDeployment(sampleUrl, cleanName);
  };

  const cancelDeployment = () => {
    stopPolling();
    setDeploying(false);
    setStatusMessage('⏹️ Deployment cancelled');
    setDeploymentProgress('');
  };

  const handleCopyWebhookUrl = () => {
    const canonicalWebhookUrl = typeof window !== 'undefined' && window.location?.origin
      ? `${window.location.origin.replace(/^http:\/\//i, 'https://')}/webhook`
      : 'https://3-239-41-33.sslip.io/webhook';
    if (navigator?.clipboard?.writeText) {
      navigator.clipboard.writeText(canonicalWebhookUrl);
      setCopiedUrl(true);
      setTimeout(() => setCopiedUrl(false), 2500);
    }
  };

  const handleVerifyWebhook = async () => {
    try {
      setVerifyingWebhook(true);
      const res = await apiService.getWebhookDeliveries(5);
      if (res.data && res.data.success) {
        setWebhookDeliveries(res.data.data || []);
      }
    } catch (err) {
      console.error('Error fetching webhook deliveries:', err);
    } finally {
      setVerifyingWebhook(false);
    }
  };

  const sampleRepositories = [
    {
      name: 'Simple Nginx',
      containerName: 'simple-nginx',
      url: 'https://github.com/nginx/nginx',
      description: 'Basic Nginx web server'
    },
    {
      name: 'Node.js Demo',
      containerName: 'nodejs-demo',
      url: 'https://github.com/nodejs/docker-node',
      description: 'Official Node.js Docker sample'
    },
    {
      name: 'React App',
      containerName: 'react-app',
      url: 'https://github.com/facebook/create-react-app',
      description: 'Create React App template'
    }
  ];

  return (
    <section className="section">
      <div className="section-header">
        <h2>🚀 Quick Deploy</h2>
        <p className="section-subtitle">Deploy containers directly from GitHub repositories</p>
      </div>
      
      <div className="deployment-card">
        <div className="deployment-form">
          <div className="form-row">
            <div className="form-group" style={{ flex: '2 1 240px' }}>
              <label htmlFor="github-url">GitHub Repository URL</label>
              <input
                type="text"
                id="github-url"
                value={githubUrl}
                onChange={(e) => handleUrlChange(e.target.value)}
                className="form-input"
                placeholder="https://github.com/username/repository"
                disabled={deploying}
              />
            </div>

            <div className="form-group" style={{ flex: '1 1 120px' }}>
              <label htmlFor="git-branch">Branch</label>
              <input
                type="text"
                id="git-branch"
                value={branch}
                onChange={(e) => setBranch(e.target.value)}
                className="form-input"
                placeholder="main"
                disabled={deploying}
              />
            </div>

            <div className="form-group" style={{ flex: '1 1 160px' }}>
              <label htmlFor="app-name">Container Name</label>
              <input
                type="text"
                id="app-name"
                value={appName}
                onChange={(e) => setAppName(sanitizeAppName(e.target.value))}
                className="form-input"
                placeholder="my-app"
                disabled={deploying}
              />
            </div>
          </div>

          <div className="deploy-actions">
            <button 
              className={`btn btn-primary deploy-btn ${deploying ? 'loading' : ''}`}
              onClick={() => triggerDeployment()}
              disabled={deploying || !githubUrl}
            >
              <span className="btn-icon">
                {deploying ? '🔄' : '🚀'}
              </span>
              {deploying ? 'Deploying...' : 'Deploy Now'}
            </button>
            
            {deploying && (
              <button 
                className="btn btn-secondary cancel-btn"
                onClick={cancelDeployment}
              >
                <span className="btn-icon">⏹️</span>
                Cancel
              </button>
            )}
          </div>

          {/* Progress and Status Messages */}
          {deploymentProgress && (
            <div className="progress-container">
              <div className="progress-text">{deploymentProgress}</div>
              {deploying && (
                <div className="progress-bar">
                  <div className="progress-fill"></div>
                </div>
              )}
            </div>
          )}

          {statusMessage && (
            <div className={`status-message ${
              statusMessage.includes('✅') ? 'success' : 
              statusMessage.includes('🚀') ? 'info' : 
              statusMessage.includes('⏳') ? 'warning' : 
              'error'
            }`}>
              {statusMessage}
            </div>
          )}

          {deploymentResult && (
            <DeploymentResultCard 
              result={deploymentResult} 
              onDismiss={() => setDeploymentResult(null)} 
            />
          )}
        </div>

        {/* Sample Repositories */}
        <div className="sample-repositories">
          <h4>🎯 Try Sample Repositories</h4>
          <div className="sample-grid">
            {sampleRepositories.map((repo, index) => (
              <div key={index} className="sample-card">
                <h5>{repo.name}</h5>
                <p>{repo.description}</p>
                <button 
                  className="btn btn-outline sample-btn"
                  onClick={() => triggerSampleDeployment(repo.url, repo.containerName)}
                  disabled={deploying}
                >
                  Deploy {repo.name}
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Deployment Information */}
        <div className="deployment-info">
          <div className="info-section">
            <h5>💡 How it works</h5>
            <ul>
              <li>Paste any public GitHub repository URL or click a sample</li>
              <li>Provide your Admin API Key if configured on the backend</li>
              <li>Auto-detects Dockerfile or Containerfile (or builds demo template)</li>
              <li>Deploys container with auto-port mapping and health check</li>
              <li>Appears in your containers list instantly</li>
            </ul>
          </div>

          <div className="info-section">
            <h5>⚡ Quick Tips</h5>
            <ul>
              <li>Your Admin API Key is stored securely in your browser session</li>
              <li>First deployment may take 1-2 minutes to pull layers</li>
              <li>No Dockerfile? AutoPod creates a clean demo container</li>
              <li>Refresh containers list or check Dashboard overview</li>
            </ul>
          </div>
        </div>
      </div>
    </section>
  );
};

export default DeploymentSection;