import React from 'react';
import { Play, Square, RotateCcw, Trash2, ExternalLink, Loader, Bug, Download } from 'lucide-react';
import { apiService } from '../services/api';
import '../styles/ContainerGrid.css';

const ContainerGrid = ({ 
  containers, 
  showActions = true, 
  onContainerAction,
  onContainerSelect,
  selectedContainer,
  loadingActions = {} // Track loading states for individual containers
}) => {
  const [downloading, setDownloading] = React.useState({});
  
  const getStatusBadge = (status) => {
    const statusLower = status.toLowerCase();
    if (statusLower.includes('running') || statusLower.includes('up')) {
      return 'badge-running';
    } else if (statusLower.includes('exited') || statusLower.includes('stopped')) {
      return 'badge-stopped';
    } else if (statusLower.includes('created')) {
      return 'badge-created';
    } else if (statusLower.includes('paused')) {
      return 'badge-paused';
    } else {
      return 'badge-unknown';
    }
  };

  const getStatusText = (status) => {
    const statusLower = status.toLowerCase();
    if (statusLower.includes('running') || statusLower.includes('up')) {
      return 'Running';
    } else if (statusLower.includes('exited') || statusLower.includes('stopped')) {
      return 'Stopped';
    } else if (statusLower.includes('created')) {
      return 'Created';
    } else if (statusLower.includes('paused')) {
      return 'Paused';
    } else {
      return status;
    }
  };

  const formatDate = (dateString) => {
    try {
      if (typeof dateString === 'number') {
        return new Date(dateString * 1000).toLocaleString();
      }
      return new Date(dateString).toLocaleString();
    } catch {
      return 'Unknown';
    }
  };

  const handleAction = (action, containerName, container, e) => {
    e.stopPropagation();
    console.log(`🔄 ContainerGrid: ${action} action for container:`, {
      containerName,
      actualNames: container.Names,
      container_name: container.container_name,
      status: container.Status,
      state: container.State,
      target_node: container.target_node
    });
    
    if (onContainerAction) {
      onContainerAction(action, containerName, container);
    }
  };

  const handleContainerClick = (container) => {
    console.log('🔍 Container clicked:', {
      names: container.Names,
      container_name: container.container_name,
      status: container.Status,
      state: container.State
    });
    
    if (onContainerSelect) {
      onContainerSelect(container);
    }
  };

  const isActionLoading = (containerName, action) => {
    return loadingActions[`${containerName}_${action}`] || false;
  };

  // Function to get exact container name for display and actions
  const getExactContainerName = (container) => {
    // Try different possible name fields
    return container.Names?.[0] || 
           container.container_name || 
           container.Name || 
           'Unknown Container';
  };

  // Function to check if container is AutoPod platform infrastructure
  const isProtectedInfrastructure = (container) => {
    const name = getExactContainerName(container).toLowerCase().replace(/^\//, '');
    return Boolean(container.is_protected) || ['autopod-frontend', 'autopod-backend', 'autopod-webhook-proxy'].includes(name);
  };

  // Function to check if container is an application container
  const isGitHubContainer = (container) => {
    if (isProtectedInfrastructure(container)) return false;
    const name = getExactContainerName(container).toLowerCase();
    return name.includes('autopod-') || name.includes('sweet-treats') || name.includes('voltride') || name.includes('crypto') || name.includes('demo') || Boolean(container.public_url);
  };

  // Debug function to log container details
  const debugContainer = (container, e) => {
    e.stopPropagation();
    const exactName = getExactContainerName(container);
    console.log('🐛 Container Debug Info:', {
      exactName,
      allNames: container.Names,
      container_name: container.container_name,
      status: container.Status,
      state: container.State,
      id: container.Id,
      image: container.Image,
      isGitHub: isGitHubContainer(container),
      ports: container.Ports
    });
    
    // Show alert with basic info
    alert(`Container Debug Info:\n\nName: ${exactName}\nStatus: ${container.Status}\nState: ${container.State}\nGitHub Container: ${isGitHubContainer(container) ? 'Yes' : 'No'}\n\nCheck browser console for full details.`);
  };

  const handleOpenWeb = async (container, e) => {
    e.stopPropagation();
    const exactName = getExactContainerName(container);
    
    // Check if public_url or web_url already resolved on the container object
    let targetUrl = container.public_url || (container.web_url && !container.web_url.includes('localhost') && !container.web_url.includes('127.0.0.1') ? container.web_url : null);
    
    // If not present on container object, query backend endpoint
    if (!targetUrl) {
      try {
        const response = await apiService.getContainerWebUrl(exactName);
        if (response?.data?.success && response.data.data?.web_url) {
          const resolved = response.data.data.web_url;
          if (!resolved.includes('localhost') && !resolved.includes('127.0.0.1')) {
            targetUrl = resolved;
          }
        }
      } catch (err) {
        if (err.response?.status === 409 || err.response?.data?.status_code === 409) {
          const msg = err.response?.data?.error || `Public routing unavailable for "${exactName}" (HTTP 409).`;
          alert(`⚠️ Routing Unavailable:\n\n${msg}`);
          return;
        }
        console.warn(`Could not resolve public web URL for ${exactName}:`, err);
      }
    }

    if (targetUrl) {
      console.log(`🌐 Opening public web interface for ${exactName}: ${targetUrl}`);
      window.open(targetUrl, '_blank', 'noopener,noreferrer');
    } else {
      console.info(`ℹ️ Container "${exactName}" is an internal service without a configured public application URL.`);
      alert(`⚠️ Routing Unavailable:\n\nContainer "${exactName}" is an internal service without a configured public application URL.`);
    }
  };

  const handleDownloadImage = async (container, e) => {
    e.stopPropagation();
    const exactName = getExactContainerName(container);
    try {
      setDownloading(prev => ({ ...prev, [exactName]: true }));
      const response = await apiService.downloadContainerImage(exactName);
      if (response?.data?.success && response.data.data?.download_url) {
        const { download_url, filename } = response.data.data;
        const link = document.createElement('a');
        link.href = download_url;
        link.setAttribute('download', filename || `${exactName}.tar`);
        document.body.appendChild(link);
        link.click();
        link.remove();
      } else {
        alert(`⚠️ Export Failed: ${response?.data?.error || 'Unknown error exporting image'}`);
      }
    } catch (err) {
      const msg = err.response?.data?.error || err.message || 'Error exporting container image';
      alert(`⚠️ Export Error:\n\n${msg}`);
    } finally {
      setDownloading(prev => ({ ...prev, [exactName]: false }));
    }
  };

  if (!containers || containers.length === 0) {
    return (
      <div className="empty-state">
        <div className="empty-icon">📦</div>
        <h3>No Containers Found</h3>
        <p>No containers are currently running or available.</p>
      </div>
    );
  }

  return (
    <div className="containers-grid">
      {containers.map((container, index) => {
        const exactName = getExactContainerName(container);
        const containerId = container.Id || container.id || `container-${index}`;
        const status = container.Status || container.status || 'Unknown';
        const image = container.Image || container.image || 'N/A';
        const created = container.Created || container.created_at;
        const ports = container.Ports || [];
        const state = container.State || 'unknown';
        
        const isRunning = status.toLowerCase().includes('running') || state.toLowerCase() === 'running';
        const isStopped = status.toLowerCase().includes('exited') || 
                         status.toLowerCase().includes('stopped') || 
                         state.toLowerCase() === 'exited';
        const isCreated = status.toLowerCase().includes('created') || state.toLowerCase() === 'created';

        const isSelected = selectedContainer && 
          (selectedContainer.Names?.[0] === exactName || 
           selectedContainer.container_name === exactName);

        const isGitHub = isGitHubContainer(container);
        const isProtected = isProtectedInfrastructure(container);
        const hostType = container.host_type || (isProtected ? 'control-plane' : 'worker');
        const targetNode = container.target_node || (hostType === 'worker' ? 'i-086803910a4c52091' : 'i-0c33cc0207c451646');
        const appName = container.application_name || (isProtected ? 'AutoPod Platform Core' : exactName.replace(/^autopod-/, '').replace(/-container$/, ''));
        const workerPort = container.worker_port || (ports.length > 0 ? (ports[0].PublicPort || ports[0].HostPort) : null);
        const startedAt = container.started_at;
        const uptime = container.uptime || container.Status || status;
        const lifecycleAction = container.lifecycle_status || (isRunning ? 'Active' : 'Stopped');
        const executionHost = container.is_worker ? `Worker: ${targetNode} (${container.node_ip || '10.0.1.107'})` : 'Control Plane: i-0c33cc0207c451646';
        
        // Prevent conflicting operations: disable all actions on container if ANY action is in-flight for it
        const isContainerBusy = Object.keys(loadingActions).some(k => k.startsWith(exactName) && loadingActions[k]);

        return (
          <div 
            key={containerId}
            className={`container-card ${isSelected ? 'selected' : ''} ${isGitHub ? 'github-container' : ''}`}
            onClick={() => handleContainerClick(container)}
          >
            <div className="container-header">
              <div className="container-name-section">
                <div className="container-icon">
                  {isProtected ? '⚙️' : (isGitHub ? '🚀' : '🐳')}
                </div>
                <div className="container-name-info">
                  <div className="container-name" title={exactName} style={{ fontSize: '1rem', fontWeight: 'bold' }}>
                    {appName}
                  </div>
                  <div className="container-subname" style={{ fontSize: '11px', color: '#64748b', fontFamily: 'monospace' }}>
                    {exactName}
                  </div>
                  <div className="container-badges-row" style={{ display: 'flex', gap: '6px', marginTop: '6px', flexWrap: 'wrap' }}>
                    {isProtected ? (
                      <span className="container-badge" style={{ background: '#475569', color: '#fff', fontSize: '11px', padding: '2px 7px', borderRadius: '4px', fontWeight: '500' }}>
                        🔒 Core Infrastructure
                      </span>
                    ) : (
                      <span className="container-badge" style={{ background: '#2563eb', color: '#fff', fontSize: '11px', padding: '2px 7px', borderRadius: '4px', fontWeight: '500' }}>
                        🚀 Worker Application
                      </span>
                    )}
                    <span className="container-badge" style={{ background: hostType === 'worker' ? '#059669' : '#7c3aed', color: '#fff', fontSize: '11px', padding: '2px 7px', borderRadius: '4px', fontWeight: '500' }}>
                      {executionHost}
                    </span>
                  </div>
                </div>
              </div>
              <div className="header-right">
                <span className={`status-badge ${getStatusBadge(status)}`}>
                  {getStatusText(status)}
                </span>
                <button 
                  className="debug-btn"
                  onClick={(e) => debugContainer(container, e)}
                  title="Debug Container Info"
                >
                  <Bug size={12} />
                </button>
              </div>
            </div>

            <div className="container-details">
              <div className="detail-item">
                <span className="detail-label">Application:</span>
                <span className="detail-value" style={{ fontWeight: '600', color: '#0f172a' }}>{appName}</span>
              </div>

              <div className="detail-item">
                <span className="detail-label">Container Name:</span>
                <span className="detail-value font-mono" style={{ fontSize: '12px' }}>{exactName}</span>
              </div>

              <div className="detail-item">
                <span className="detail-label">Execution Host:</span>
                <span className="detail-value">{executionHost}</span>
              </div>

              <div className="detail-item">
                <span className="detail-label">Status & Uptime:</span>
                <span className="detail-value" style={{ fontWeight: '500' }}>{status}</span>
              </div>

              {workerPort && (
                <div className="detail-item">
                  <span className="detail-label">Worker Port:</span>
                  <span className="detail-value" style={{ color: '#0284c7', fontWeight: '600' }}>
                    Port {workerPort} &rarr; 80
                  </span>
                </div>
              )}

              <div className="detail-item">
                <span className="detail-label">Last Started:</span>
                <span className="detail-value" style={{ color: '#059669', fontWeight: '500' }}>
                  {startedAt ? formatDate(startedAt) : formatDate(created)}
                </span>
              </div>

              <div className="detail-item">
                <span className="detail-label">Created Time:</span>
                <span className="detail-value">{formatDate(created)}</span>
              </div>

              <div className="detail-item">
                <span className="detail-label">Lifecycle State:</span>
                <span className="detail-value" style={{ textTransform: 'capitalize' }}>{lifecycleAction}</span>
              </div>

              <div className="detail-item">
                <span className="detail-label">Image:</span>
                <span className="detail-value image-name" title={image}>
                  {image.length > 32 ? `${image.substring(0, 32)}...` : image}
                </span>
              </div>
              
              {ports.length > 0 && (
                <div className="detail-item">
                  <span className="detail-label">Port Mappings:</span>
                  <span className="detail-value ports">
                    {ports.map((port, idx) => (
                      <span key={idx} className="port-mapping" style={{ fontSize: '11px', background: '#f1f5f9', padding: '1px 5px', borderRadius: '3px' }}>
                        {port.PublicPort || port.HostPort || 'N/A'}:{port.PrivatePort || port.ContainerPort || 'N/A'}
                      </span>
                    ))}
                  </span>
                </div>
              )}
            </div>

            {showActions && (
              <div className="container-actions">
                {/* Start Button - Show for stopped/created containers */}
                {(isStopped || isCreated) && (
                  <button 
                    className="action-btn btn-success btn-sm"
                    onClick={(e) => handleAction('start', exactName, container, e)}
                    disabled={isContainerBusy}
                    title={isContainerBusy ? "Operation in progress" : "Start Container"}
                  >
                    {isActionLoading(exactName, 'start') ? (
                      <Loader size={14} className="spin" />
                    ) : (
                      <Play size={14} />
                    )}
                    <span>
                      {isActionLoading(exactName, 'start') ? 'Starting...' : 'Start'}
                    </span>
                  </button>
                )}
                
                {/* Stop Button - Show for running containers */}
                {isRunning && (
                  <button 
                    className="action-btn btn-warning btn-sm"
                    onClick={(e) => handleAction('stop', exactName, container, e)}
                    disabled={isContainerBusy}
                    title={isContainerBusy ? "Operation in progress" : "Stop Container"}
                  >
                    {isActionLoading(exactName, 'stop') ? (
                      <Loader size={14} className="spin" />
                    ) : (
                      <Square size={14} />
                    )}
                    <span>
                      {isActionLoading(exactName, 'stop') ? 'Stopping...' : 'Stop'}
                    </span>
                  </button>
                )}
                
                {/* Restart Button - Show for running containers */}
                {isRunning && (
                  <button 
                    className="action-btn btn-secondary btn-sm"
                    onClick={(e) => handleAction('restart', exactName, container, e)}
                    disabled={isContainerBusy}
                    title={isContainerBusy ? "Operation in progress" : "Restart Container"}
                  >
                    {isActionLoading(exactName, 'restart') ? (
                      <Loader size={14} className="spin" />
                    ) : (
                      <RotateCcw size={14} />
                    )}
                    <span>
                      {isActionLoading(exactName, 'restart') ? 'Restarting...' : 'Restart'}
                    </span>
                  </button>
                )}
                
                {/* Remove Button - Safeguard infrastructure containers */}
                {isProtected ? (
                  <button 
                    className="action-btn btn-secondary btn-sm"
                    disabled
                    title="Protected AutoPod infrastructure container cannot be removed"
                    style={{ opacity: 0.5, cursor: 'not-allowed' }}
                  >
                    <Trash2 size={14} />
                    <span>Protected</span>
                  </button>
                ) : (
                  <button 
                    className="action-btn btn-danger btn-sm"
                    onClick={(e) => handleAction('remove', exactName, container, e)}
                    disabled={isContainerBusy}
                    title={isContainerBusy ? "Operation in progress" : (isRunning ? "Remove Container (will stop first)" : "Remove Container")}
                  >
                    {isActionLoading(exactName, 'remove') ? (
                      <Loader size={14} className="spin" />
                    ) : (
                      <Trash2 size={14} />
                    )}
                    <span>
                      {isActionLoading(exactName, 'remove') ? 'Removing...' : 'Remove'}
                    </span>
                  </button>
                )}

                {/* Open Web Interface */}
                {isRunning && (ports.length > 0 || container.public_url || container.web_url) && (
                  <button 
                    className="action-btn btn-primary btn-sm"
                    onClick={(e) => handleOpenWeb(container, e)}
                    disabled={isContainerBusy}
                    title={container.public_url ? `Open live application (${container.public_url})` : "Public ingress route"}
                  >
                    <ExternalLink size={14} />
                    <span>Open</span>
                  </button>
                )}

                {/* Download Docker Image (.tar) Button */}
                <button 
                  className="action-btn btn-secondary btn-sm"
                  onClick={(e) => handleDownloadImage(container, e)}
                  disabled={isContainerBusy || Boolean(downloading[exactName])}
                  title="Download Docker image (.tar archive via S3)"
                >
                  {downloading[exactName] ? (
                    <Loader size={14} className="spin" />
                  ) : (
                    <Download size={14} />
                  )}
                  <span>
                    {downloading[exactName] ? 'Saving...' : 'Download (.tar)'}
                  </span>
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
};

export default ContainerGrid;